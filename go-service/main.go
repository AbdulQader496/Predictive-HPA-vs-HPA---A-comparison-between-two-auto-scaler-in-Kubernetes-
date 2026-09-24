package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	_ "github.com/mattn/go-sqlite3"
	"github.com/redis/go-redis/v9"
)

var db *sql.DB
var rdb *redis.Client
var ctx = context.Background()

// pythonServiceURL can be overridden by PYTHON_SERVICE_URL environment variable (kept for backward compatibility)
var pythonServiceURL = getEnv("PYTHON_SERVICE_URL", "http://localhost:5000")

// publicBaseURL is the externally-reachable address used when building short
// URLs returned to clients. Defaults to localhost for local/dev use; must be
// set to the actual reachable host:port (e.g. via NodePort or port-forward)
// for links to work from outside the cluster. See PUBLIC_BASE_URL env var.
var publicBaseURL = getEnv("PUBLIC_BASE_URL", "http://localhost:8000")

// cpuLoadIterations controls how many SHA-256 rounds are computed per redirect request.
// This is the knob you tune during Step 12 (workload calibration) to make the service
// genuinely CPU-bound under load rather than purely I/O-bound. Override via CPU_LOAD_ITERATIONS.
var cpuLoadIterations = getEnvInt("CPU_LOAD_ITERATIONS", 20000)

// seedCount controls how many deterministic short-code/long-url pairs are pre-seeded
// into go.db on startup. Because the seeding logic is deterministic (same codes, same
// order, same content every time), every replica ends up with identical local data
// without needing a shared volume or write coordination across pods. Override via SEED_COUNT.
var seedCount = getEnvInt("SEED_COUNT", 1000)

type ShortenRequest struct {
	LongURL string `json:"long_url" binding:"required"`
}

type ShortenResponse struct {
	ShortCode string `json:"short_code"`
	ShortURL  string `json:"short_url"`
	LongURL   string `json:"long_url"`
}

type ClickEvent struct {
	ShortCode string `json:"short_code"`
	ClickedAt string `json:"clicked_at"`
}

func initDB() {
	var err error
	db, err = sql.Open("sqlite3", "./go.db")
	if err != nil {
		log.Fatal(err)
	}

	createTableSQL := `CREATE TABLE IF NOT EXISTS urls (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		short_code TEXT UNIQUE NOT NULL,
		long_url TEXT NOT NULL,
		created_at DATETIME DEFAULT CURRENT_TIMESTAMP
	);`

	_, err = db.Exec(createTableSQL)
	if err != nil {
		log.Fatal(err)
	}

	log.Println("Database initialized successfully")

	seedDatabase()
}

// seedDatabase inserts a fixed, deterministic set of short_code -> long_url pairs
// if the table is currently empty. Because the codes and URLs are generated the
// same way every time (loadtest0001, loadtest0002, ...), every pod that runs this
// on startup ends up with byte-identical data - no shared volume, no write
// contention across replicas, and load tests can target these fixed codes without
// ever hitting the write path (POST /api/shorten) during a measured run.
func seedDatabase() {
	var count int
	err := db.QueryRow("SELECT COUNT(*) FROM urls").Scan(&count)
	if err != nil {
		log.Printf("Warning: could not check seed status: %v", err)
		return
	}
	if count > 0 {
		log.Printf("Database already contains %d rows, skipping seed", count)
		return
	}

	tx, err := db.Begin()
	if err != nil {
		log.Printf("Warning: could not start seed transaction: %v", err)
		return
	}

	stmt, err := tx.Prepare("INSERT INTO urls (short_code, long_url) VALUES (?, ?)")
	if err != nil {
		log.Printf("Warning: could not prepare seed statement: %v", err)
		tx.Rollback()
		return
	}
	defer stmt.Close()

	for i := 1; i <= seedCount; i++ {
		code := fmt.Sprintf("loadtest%04d", i)
		url := fmt.Sprintf("https://example.com/page/%04d", i)
		if _, err := stmt.Exec(code, url); err != nil {
			log.Printf("Warning: failed to seed row %d: %v", i, err)
		}
	}

	if err := tx.Commit(); err != nil {
		log.Printf("Warning: failed to commit seed transaction: %v", err)
		return
	}

	log.Printf("Seeded %d deterministic short URLs (loadtest0001 .. loadtest%04d)", seedCount, seedCount)
}

func initRedis() {
	redisURL := getEnv("REDIS_URL", "localhost:6380")
	
	rdb = redis.NewClient(&redis.Options{
		Addr:     redisURL,
		Password: "", // no password
		DB:       0,  // default DB
	})

	// Test connection
	_, err := rdb.Ping(ctx).Result()
	if err != nil {
		log.Printf("Warning: Redis connection failed: %v. Events will not be published.", err)
		rdb = nil
	} else {
		log.Printf("Redis connected successfully at %s", redisURL)
	}
}

func getEnv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if value := os.Getenv(key); value != "" {
		if parsed, err := strconv.Atoi(value); err == nil {
			return parsed
		}
		log.Printf("Warning: invalid int for %s=%q, using fallback %d", key, value, fallback)
	}
	return fallback
}

func generateShortCode() string {
	b := make([]byte, 6)
	rand.Read(b)
	encoded := base64.URLEncoding.EncodeToString(b)
	// Take first 6 characters and remove any special chars
	shortCode := encoded[:6]
	return shortCode
}

// burnCPU performs a fixed number of SHA-256 rounds. This is the deliberate,
// tunable CPU cost injected into the redirect path (see cpuLoadIterations).
// A URL shortener's redirect logic is naturally I/O-bound (cache/DB lookup),
// so without an explicit compute step, CPU utilization would stay flat under
// load and give HPA/PHPA nothing meaningful to react to. Tune via
// CPU_LOAD_ITERATIONS during Step 12 workload calibration.
func burnCPU(iterations int) {
	sum := sha256.Sum256([]byte("phpa-vs-hpa-cpu-load-seed"))
	for i := 0; i < iterations; i++ {
		sum = sha256.Sum256(sum[:])
	}
}

func createShortURL(c *gin.Context) {
	var req ShortenRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	shortCode := generateShortCode()

	// Check if short code already exists (unlikely but possible)
	var exists int
	err := db.QueryRow("SELECT COUNT(*) FROM urls WHERE short_code = ?", shortCode).Scan(&exists)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Database error"})
		return
	}

	// Regenerate if exists (very rare)
	for exists > 0 {
		shortCode = generateShortCode()
		db.QueryRow("SELECT COUNT(*) FROM urls WHERE short_code = ?", shortCode).Scan(&exists)
	}

	_, err = db.Exec("INSERT INTO urls (short_code, long_url) VALUES (?, ?)", shortCode, req.LongURL)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create short URL"})
		return
	}

	response := ShortenResponse{
		ShortCode: shortCode,
		ShortURL:  publicBaseURL + "/" + shortCode,
		LongURL:   req.LongURL,
	}

	log.Printf("Created short URL: %s -> %s", shortCode, req.LongURL)
	c.JSON(http.StatusOK, response)
}

func redirect(c *gin.Context) {
	shortCode := c.Param("code")
	var longURL string

	// Deliberate CPU cost applied to every redirect request, regardless of
	// cache hit or miss - this is what makes the endpoint CPU-bound for the
	// autoscaling experiment. See burnCPU() and cpuLoadIterations.
	burnCPU(cpuLoadIterations)

	// Try Redis cache first (if available)
	if rdb != nil {
		cachedURL, err := rdb.Get(ctx, "url:"+shortCode).Result()
		if err == nil {
			log.Printf("Cache hit for %s", shortCode)
			longURL = cachedURL
			// Publish click event to Redis
			go publishClickEvent(shortCode)
			c.Redirect(http.StatusMovedPermanently, longURL)
			return
		}
	}

	// Cache miss or Redis unavailable - query database
	err := db.QueryRow("SELECT long_url FROM urls WHERE short_code = ?", shortCode).Scan(&longURL)
	if err != nil {
		if err == sql.ErrNoRows {
			c.JSON(http.StatusNotFound, gin.H{"error": "Short URL not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Database error"})
		return
	}

	// Cache the URL in Redis (1 hour TTL)
	if rdb != nil {
		rdb.Set(ctx, "url:"+shortCode, longURL, 1*time.Hour)
		log.Printf("Cached URL for %s", shortCode)
	}

	// Publish click event to Redis (or fallback to HTTP)
	go publishClickEvent(shortCode)

	// Redirect to the long URL
	c.Redirect(http.StatusMovedPermanently, longURL)
}

func publishClickEvent(shortCode string) {
	event := ClickEvent{
		ShortCode: shortCode,
		ClickedAt: time.Now().Format(time.RFC3339),
	}

	// Try Redis Pub/Sub first
	if rdb != nil {
		jsonData, err := json.Marshal(event)
		if err != nil {
			log.Printf("Error marshaling event: %v", err)
			return
		}

		err = rdb.Publish(ctx, "click_events", jsonData).Err()
		if err != nil {
			log.Printf("Redis publish error: %v, falling back to HTTP", err)
			// Fallback to HTTP if Redis fails
			sendClickEventHTTP(shortCode)
		} else {
			log.Printf("✅ Click event published to Redis: %s", shortCode)
		}
	} else {
		// No Redis available, use HTTP fallback
		sendClickEventHTTP(shortCode)
	}
}

func sendClickEventHTTP(shortCode string) {
	event := ClickEvent{
		ShortCode: shortCode,
		ClickedAt: time.Now().Format(time.RFC3339),
	}

	jsonData, err := json.Marshal(event)
	if err != nil {
		log.Printf("Error marshaling event: %v", err)
		return
	}

	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Post(pythonServiceURL+"/api/events", "application/json", bytes.NewBuffer(jsonData))
	if err != nil {
		log.Printf("Error sending event to Python service: %v", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("Python service returned status: %d", resp.StatusCode)
	} else {
		log.Printf("Click event sent via HTTP for: %s", shortCode)
	}
}

// healthCheck is used by the Kubernetes readiness and liveness probes.
// It checks that the database is actually reachable rather than just
// returning 200 unconditionally, so a pod with a broken DB connection
// correctly gets marked NotReady instead of receiving traffic.
func healthCheck(c *gin.Context) {
	if err := db.Ping(); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"status": "unhealthy",
			"error":  "database unreachable",
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"status":  "healthy",
		"service": "go-redirect-service",
	})
}

func main() {
	initDB()
	defer db.Close()

	initRedis()
	if rdb != nil {
		defer rdb.Close()
	}

	r := gin.Default()

	// CORS middleware
	r.Use(func(c *gin.Context) {
		c.Writer.Header().Set("Access-Control-Allow-Origin", "*")
		c.Writer.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		c.Writer.Header().Set("Access-Control-Allow-Headers", "Content-Type")

		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(http.StatusOK)
			return
		}

		c.Next()
	})

	// Routes
	r.GET("/health", healthCheck)
	r.POST("/api/shorten", createShortURL)
	r.GET("/:code", redirect)

	log.Println("Go service starting on :8000")
	r.Run(":8000")
}
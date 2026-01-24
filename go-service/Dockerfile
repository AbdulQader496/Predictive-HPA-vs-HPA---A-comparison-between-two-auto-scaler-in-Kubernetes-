FROM golang:1.24 AS build

WORKDIR /app

# Enable CGO for sqlite3
ENV CGO_ENABLED=1

# Install required system dependencies for CGO + SQLite
RUN apt-get update && apt-get install -y \
    gcc \
    libc6-dev \
    sqlite3 \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy go.mod first
COPY go.mod ./

# Copy source code
COPY . .

# Download deps and generate go.sum
RUN go mod tidy

# Build the binary (CGO enabled)
RUN go build -o go-service

# Runtime image
FROM debian:bookworm-slim

WORKDIR /app

# Install sqlite runtime library
RUN apt-get update && apt-get install -y \
    sqlite3 \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /app/go-service .

EXPOSE 8000

CMD ["./go-service"]

import http from 'k6/http';
import { check, sleep } from 'k6';

// BASE_URL defaults to the in-cluster Service DNS name. Override with
// k6 run -e BASE_URL=http://<external-host>:<port> ... when running externally.
const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';

// Fixed, deterministic set of seeded short codes (see seedDatabase() in main.go).
// Only reading these - never hits the write path (POST /api/shorten).
const SEED_COUNT = 1000;

export const options = {
  // Simple constant-load pilot config. Replace with the three finalised
  // scenarios (steady / spike / fluctuating) in Step 13.
  vus: 20,
  duration: '90s',
};

function randomShortCode() {
  const n = Math.floor(Math.random() * SEED_COUNT) + 1;
  return 'loadtest' + String(n).padStart(4, '0');
}

export default function () {
  const code = randomShortCode();
  const url = `${BASE_URL}/${code}`;

  // Critical fix: do NOT follow the redirect. We are measuring go-service's
  // own response, not whatever the redirect target happens to be. Without
  // this, k6 (like the earlier `hey` test) would follow the 301 out to
  // https://example.com and measure that instead - producing meaningless
  // latency/error data.
  const res = http.get(url, { redirects: 0 });

  check(res, {
    'status is 301': (r) => r.status === 301,
    'has Location header': (r) => r.headers['Location'] !== undefined,
  });

  sleep(0.1);
}
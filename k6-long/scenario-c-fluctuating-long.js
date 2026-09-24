import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario C (LONG variant): fluctuating load with each stage doubled to
// 60 seconds (from 30s), giving each load level more time to actually
// influence the CPU/replica-history signal before the next transition -
// particularly relevant for PHPA, whose model is fit against a rolling
// history that needs enough samples per stage to respond meaningfully.
// Total duration: 6 minutes.
export const options = {
  stages: [
    { duration: '60s', target: 5 },    // low
    { duration: '60s', target: 15 },   // medium
    { duration: '60s', target: 30 },   // high
    { duration: '60s', target: 5 },    // low
    { duration: '60s', target: 30 },   // high
    { duration: '60s', target: 5 },    // low
  ],
};

function randomShortCode() {
  const n = Math.floor(Math.random() * SEED_COUNT) + 1;
  return 'loadtest' + String(n).padStart(4, '0');
}

export default function () {
  const url = `${BASE_URL}/${randomShortCode()}`;
  const res = http.get(url, { redirects: 0 });
  check(res, {
    'status is 301': (r) => r.status === 301,
    'has Location header': (r) => r.headers['Location'] !== undefined,
  });
  sleep(0.1);
}
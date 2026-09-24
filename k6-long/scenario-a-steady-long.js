import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario A (LONG variant): steady load with an extended hold period
// (6 minutes instead of 3) to capture more steady-state behaviour and give
// autoscalers more opportunity to fully settle between scale events.
// Total duration: ~7 minutes.
export const options = {
  stages: [
    { duration: '30s', target: 5 },   // warm-up
    { duration: '6m', target: 5 },    // extended constant moderate load
    { duration: '30s', target: 0 },   // cool-down
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
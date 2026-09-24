import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario C: Fluctuating load - tests repeated changes and prediction
// stability. This is the scenario most likely to expose a difference
// between HPA (reactive, no memory) and PHPA (predictive, holds replica
// history) - repeated oscillation is exactly what a Linear Regression model
// either smooths usefully or overreacts to. VU levels provisional, same
// caveat as Scenarios A and B.
export const options = {
  stages: [
    { duration: '30s', target: 5 },    // low
    { duration: '30s', target: 15 },   // medium
    { duration: '30s', target: 30 },   // high
    { duration: '30s', target: 5 },    // low
    { duration: '30s', target: 30 },   // high
    { duration: '30s', target: 5 },    // low
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
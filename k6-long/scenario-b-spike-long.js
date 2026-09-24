import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario B (LONG variant): sudden spike with an extended hold period at
// high load (4 minutes instead of 2), to observe sustained high-load
// behaviour and any longer-term divergence between HPA and PHPA once both
// have had time to fully react and stabilise at their respective replica
// counts. Total duration: ~5m10s.
export const options = {
  stages: [
    { duration: '30s', target: 5 },    // low load
    { duration: '10s', target: 30 },   // sudden jump to high
    { duration: '4m', target: 30 },    // extended hold at high load
    { duration: '30s', target: 5 },    // return to low
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
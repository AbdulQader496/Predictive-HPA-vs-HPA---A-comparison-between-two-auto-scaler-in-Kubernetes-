import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario B: Sudden spike - measures scaling delay and temporary
// degradation. The abrupt jump from 5 to 30 VUs within 10 seconds is
// deliberate; this is what tests reaction speed (the core HPA vs PHPA
// comparison point). VU levels provisional, same caveat as Scenario A.
export const options = {
  stages: [
    { duration: '30s', target: 5 },    // low load
    { duration: '10s', target: 30 },   // sudden jump to high
    { duration: '2m', target: 30 },    // hold high load
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
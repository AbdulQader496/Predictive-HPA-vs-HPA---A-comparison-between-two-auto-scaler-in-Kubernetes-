import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://go-service:8000';
const SEED_COUNT = 1000;

// Scenario A: Steady load - measures normal operation and possible
// over-provisioning. VU levels are provisional pending full calibration
// against CPU_LOAD_ITERATIONS=1500 (Step 12) - adjust 'target' values if
// observed CPU% doesn't land in a reasonable range once run against a live
// HPA/PHPA deployment.
export const options = {
  stages: [
    { duration: '30s', target: 5 },   // warm-up
    { duration: '3m', target: 5 },    // constant moderate load - kept at 5 VUs
                                       // (not 10) so this scenario stays under
                                       // the 60% CPU target and represents
                                       // genuine "normal operation", distinct
                                       // from Scenarios B/C which are meant to
                                       // push past the target
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
import http from 'k6/http';
import { sleep } from 'k6';

http.setResponseCallback(http.expectedStatuses(200));

export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ['p(95)', 'p(99)'],
  scenarios: {
    local: {
      executor: 'shared-iterations',
      vus: Number(__ENV.DYD_PROBE_VUS),
      iterations: Number(__ENV.DYD_PROBE_SAMPLES),
      maxDuration: '35s',
      gracefulStop: '0s',
    },
  },
};

export default function () {
  http.get(__ENV.DYD_PROBE_URL, { redirects: 0, timeout: '1s' });
  sleep(0.2);
}

export function handleSummary(data) {
  return { stdout: 'DYD_SUMMARY ' + JSON.stringify(data.metrics) + '\n' };
}

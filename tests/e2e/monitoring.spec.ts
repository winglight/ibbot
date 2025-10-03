import { test, expect } from '@playwright/test';
import {
  baseLogs,
  metricsSeries,
  queueSnapshot,
  stubMonitoringApis,
  tailLogs,
} from './utils/monitoringTestUtils';

test.beforeEach(async ({ page, baseURL }) => {
  await stubMonitoringApis(page);
  await page.goto(`${baseURL}/static/index.html`);
});

test('displays queue metrics, write-rate chart, and logs with tail updates', async ({ page }) => {
  await expect(page.locator('#queueMetricsTable tbody tr')).toContainText(queueSnapshot.queues[0].queue_key);
  await expect(page.locator('#streamMetricsTable tbody tr')).toContainText(queueSnapshot.streams[0].stream_key);

  await page.waitForFunction(() => {
    return window.app?.monitoringDashboard?.influxChart?.data?.datasets?.length > 0;
  });
  const labels = await page.evaluate(() => window.app.monitoringDashboard.influxChart.data.datasets.map((ds: any) => ds.label));
  expect(labels).toContain(metricsSeries[0].metric);

  await expect(page.locator('#logsContent')).toContainText(baseLogs[0].trim());
  await page.evaluate(() => {
    if (window.app?.logsManager) {
      window.app.logsManager.tailInterval = 100;
      window.app.logsManager.scheduleTail();
    }
  });
  await expect(page.locator('#logsContent')).toContainText(tailLogs[0].trim(), { timeout: 5000 });
});

import { test, expect } from '@playwright/test';

const queueSnapshot = {
  source: 'snapshot',
  timestamp: new Date('2024-05-05T10:00:00Z').toISOString(),
  queues: [
    {
      name: 'IB Commands',
      queue_key: 'queue:ib:commands',
      length: 3,
      consumption_rate: 1.5,
      stale_seconds: 12,
    },
  ],
  streams: [
    {
      name: 'Strategy Events',
      stream_key: 'stream:strategy:events',
      group: 'group:strategy',
      pending: 1,
      consumption_rate: 0.5,
      stale_seconds: 8,
    },
  ],
};

const influxSeries = [
  {
    time: new Date('2024-05-05T10:00:00Z').toISOString(),
    value: 4,
    service: 'queue-monitor',
    metric: 'ib-commands-queue-length',
  },
  {
    time: new Date('2024-05-05T10:01:00Z').toISOString(),
    value: 6,
    service: 'queue-monitor',
    metric: 'ib-commands-queue-length',
  },
];

const baseLogs = [
  '| 2024-05-05 10:00:00 | INFO | queue_manager | REQUEST Q1 processed\n',
  '| 2024-05-05 10:00:05 | ERROR | queue_manager | REQUEST Q2 failed\n',
];

const tailLogs = [
  '| 2024-05-05 10:00:10 | INFO | queue_manager | tail update delivered\n',
];

test.beforeEach(async ({ page, baseURL }) => {
  let tailDelivered = false;

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const { pathname, searchParams } = url;

    if (pathname === '/api/monitoring/queues') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(queueSnapshot),
      });
      return;
    }

    if (pathname === '/api/monitoring/influx/write-rate') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          series: influxSeries,
          summary: {
            'ib-commands-queue-length': {
              points: influxSeries.length,
              sum: influxSeries.reduce((acc, entry) => acc + entry.value, 0),
              average: influxSeries.reduce((acc, entry) => acc + entry.value, 0) / influxSeries.length,
            },
          },
        }),
      });
      return;
    }

    if (pathname === '/api/logs/modules') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ modules: ['queue_manager'] }),
      });
      return;
    }

    if (pathname === '/api/logs') {
      const direction = searchParams.get('direction') ?? 'backward';
      const cursor = searchParams.get('cursor');
      const totalBase = baseLogs.length + (tailDelivered ? tailLogs.length : 0);

      if (direction === 'forward' && !tailDelivered) {
        tailDelivered = true;
        const total = baseLogs.length + tailLogs.length;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            success: true,
            logs: tailLogs,
            pagination: {
              total,
              page_size: Number(searchParams.get('page_size') || 200),
              direction: 'forward',
              start: baseLogs.length,
              end: total,
              has_more_backward: true,
              has_more_forward: false,
              next_backward_cursor: baseLogs.length,
              next_forward_cursor: total,
              tail_cursor: total,
            },
          }),
        });
        return;
      }

      const total = totalBase || baseLogs.length;
      const start = Math.max(0, total - baseLogs.length);
      const end = total;

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          logs: baseLogs,
          pagination: {
            total,
            page_size: Number(searchParams.get('page_size') || 200),
            direction: 'backward',
            start,
            end,
            has_more_backward: false,
            has_more_forward: !tailDelivered,
            next_backward_cursor: null,
            next_forward_cursor: baseLogs.length,
            tail_cursor: total,
          },
        }),
      });
      return;
    }

    // Default stub for other API calls.
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ success: true }),
    });
  });

  await page.goto(`${baseURL}/static/index.html`);
});

test('displays queue metrics, influx chart, and logs with tail updates', async ({ page }) => {
  await expect(page.locator('#queueMetricsTable tbody tr')).toContainText('queue:ib:commands');
  await expect(page.locator('#streamMetricsTable tbody tr')).toContainText('stream:strategy:events');

  await page.waitForFunction(() => {
    return window.app?.monitoringDashboard?.influxChart?.data?.datasets?.length > 0;
  });
  const labels = await page.evaluate(() => window.app.monitoringDashboard.influxChart.data.datasets.map((ds: any) => ds.label));
  expect(labels).toContain('ib-commands-queue-length');

  await expect(page.locator('#logsContent')).toContainText('REQUEST Q1 processed');
  await page.evaluate(() => {
    if (window.app?.logsManager) {
      window.app.logsManager.tailInterval = 100;
      window.app.logsManager.scheduleTail();
    }
  });
  await expect(page.locator('#logsContent')).toContainText('tail update delivered', { timeout: 5000 });
});

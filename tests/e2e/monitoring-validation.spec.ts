import { test, expect } from '@playwright/test';
import {
  baseLogs,
  metricsSeries,
  queueSnapshot,
  stubMonitoringApis,
  tailLogs,
} from './utils/monitoringTestUtils';

const parseNumeric = (text: string) => {
  const cleaned = text.replace(/[^0-9.+-]/g, '');
  return cleaned ? Number(cleaned) : 0;
};

const toTrimmedLines = (input: string | null | undefined) =>
  (input || '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

test.describe('monitoring validation', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    await stubMonitoringApis(page);
    await page.goto(`${baseURL}/static/index.html`);
  });

  test('validates monitoring dashboards against backend data', async ({ page }, testInfo) => {
    const queueRows = page.locator('#queueMetricsTable tbody tr');
    await expect(queueRows).toHaveCount(queueSnapshot.queues.length);
    const queueUiData = await queueRows.evaluateAll((rows) =>
      rows.map((row) => {
        const cells = Array.from(row.querySelectorAll('td'));
        return {
          name: cells[0]?.textContent?.trim() ?? '',
          queueKey: cells[1]?.textContent?.trim() ?? '',
          length: cells[2]?.textContent?.trim() ?? '',
          consumption: cells[3]?.textContent?.trim() ?? '',
          stale: cells[4]?.textContent?.trim() ?? '',
        };
      }),
    );

    const queueComparisons = queueUiData.map((row) => {
      const expected = queueSnapshot.queues.find((entry) => entry.queue_key === row.queueKey);
      expect(expected, `Expected queue data for ${row.queueKey}`).toBeTruthy();
      const expectedQueue = expected!;
      const lengthDiff = parseNumeric(row.length) - expectedQueue.length;
      const consumptionDiff = parseNumeric(row.consumption) - expectedQueue.consumption_rate;
      const staleDiff = parseNumeric(row.stale) - expectedQueue.stale_seconds;

      expect(Math.abs(lengthDiff)).toBeLessThan(1e-3);
      expect(Math.abs(consumptionDiff)).toBeLessThan(1e-3);
      expect(Math.abs(staleDiff)).toBeLessThan(1e-3);

      return {
        queue: row.queueKey,
        displayed: {
          length: parseNumeric(row.length),
          consumptionRate: parseNumeric(row.consumption),
          staleSeconds: parseNumeric(row.stale),
        },
        expected: {
          length: expectedQueue.length,
          consumptionRate: expectedQueue.consumption_rate,
          staleSeconds: expectedQueue.stale_seconds,
        },
        deltas: {
          length: lengthDiff,
          consumptionRate: consumptionDiff,
          staleSeconds: staleDiff,
        },
      };
    });

    await testInfo.attach('queue-metric-comparison', {
      body: JSON.stringify(queueComparisons, null, 2),
      contentType: 'application/json',
    });

    const streamRows = page.locator('#streamMetricsTable tbody tr');
    await expect(streamRows).toHaveCount(queueSnapshot.streams.length);
    const streamUiData = await streamRows.evaluateAll((rows) =>
      rows.map((row) => {
        const cells = Array.from(row.querySelectorAll('td'));
        return {
          name: cells[0]?.textContent?.trim() ?? '',
          streamKey: cells[1]?.textContent?.trim() ?? '',
          group: cells[2]?.textContent?.trim() ?? '',
          pending: cells[3]?.textContent?.trim() ?? '',
          consumption: cells[4]?.textContent?.trim() ?? '',
          stale: cells[5]?.textContent?.trim() ?? '',
        };
      }),
    );

    const streamComparisons = streamUiData.map((row) => {
      const expected = queueSnapshot.streams.find((entry) => entry.stream_key === row.streamKey);
      expect(expected, `Expected stream data for ${row.streamKey}`).toBeTruthy();
      const expectedStream = expected!;
      const pendingDiff = parseNumeric(row.pending) - expectedStream.pending;
      const consumptionDiff = parseNumeric(row.consumption) - expectedStream.consumption_rate;
      const staleDiff = parseNumeric(row.stale) - expectedStream.stale_seconds;

      expect(row.group).toContain(expectedStream.group);
      expect(Math.abs(pendingDiff)).toBeLessThan(1e-3);
      expect(Math.abs(consumptionDiff)).toBeLessThan(1e-3);
      expect(Math.abs(staleDiff)).toBeLessThan(1e-3);

      return {
        stream: row.streamKey,
        displayed: {
          pending: parseNumeric(row.pending),
          consumptionRate: parseNumeric(row.consumption),
          staleSeconds: parseNumeric(row.stale),
        },
        expected: {
          pending: expectedStream.pending,
          consumptionRate: expectedStream.consumption_rate,
          staleSeconds: expectedStream.stale_seconds,
        },
        deltas: {
          pending: pendingDiff,
          consumptionRate: consumptionDiff,
          staleSeconds: staleDiff,
        },
      };
    });

    await testInfo.attach('stream-metric-comparison', {
      body: JSON.stringify(streamComparisons, null, 2),
      contentType: 'application/json',
    });

    await page.waitForFunction(() => {
      return window.app?.monitoringDashboard?.dbChart?.data?.datasets?.length > 0;
    });

    const chartDatasets = await page.evaluate(() => {
      const chart = window.app?.monitoringDashboard?.dbChart;
      if (!chart) {
        return [];
      }
      return chart.data.datasets.map((dataset: any) => ({
        label: dataset.label,
        data: dataset.data.map((point: any) => ({ x: point.x, y: point.y })),
      }));
    });

    const expectedMetric = metricsSeries[0].metric;
    const dataset = chartDatasets.find((entry: any) => entry.label === expectedMetric);
    expect(dataset, `Expected dataset for ${expectedMetric}`).toBeTruthy();
    const datasetData = dataset!.data.map((point: any) => ({
      time: new Date(point.x).toISOString(),
      value: Number(point.y),
    }));

    const expectedByTime = new Map(metricsSeries.map((entry) => [entry.time, entry.value]));
    const chartComparisons = datasetData.map(({ time, value }) => {
      const expectedValue = expectedByTime.get(time);
      expect(expectedValue, `Expected value for ${time}`).not.toBeUndefined();
      const diff = value - Number(expectedValue);
      expect(Math.abs(diff)).toBeLessThan(1e-6);
      return {
        metric: expectedMetric,
        time,
        displayed: value,
        expected: expectedValue,
        delta: diff,
      };
    });

    await testInfo.attach('metrics-series-comparison', {
      body: JSON.stringify(chartComparisons, null, 2),
      contentType: 'application/json',
    });

    const summaryText = await page.locator('#dbSummaryText').textContent();
    expect(summaryText).toBeTruthy();
    const averageMatch = summaryText!.match(/平均\s+([0-9.]+)\/m/);
    expect(averageMatch).not.toBeNull();
    const averageFromUi = Number(averageMatch![1]);
    const expectedAverage =
      metricsSeries.reduce((acc, entry) => acc + entry.value, 0) / metricsSeries.length;
    expect(Math.abs(averageFromUi - expectedAverage)).toBeLessThan(1e-6);

    const pointsMatch = summaryText!.match(/\((\d+)\s*点\)/);
    expect(pointsMatch).not.toBeNull();
    const pointsFromUi = Number(pointsMatch![1]);
    expect(pointsFromUi).toBe(metricsSeries.length);

    await expect(page.locator('#logsContent')).toContainText(baseLogs[0].trim());
    await page.evaluate(() => {
      if (window.app?.logsManager) {
        window.app.logsManager.tailInterval = 100;
        window.app.logsManager.scheduleTail();
      }
    });
    await expect(page.locator('#logsContent')).toContainText(tailLogs[0].trim(), { timeout: 5000 });

    const logsText = await page.locator('#logsContent').textContent();
    const displayedLogs = toTrimmedLines(logsText);
    const expectedLogs = [...baseLogs, ...tailLogs].map((line) => line.trim());
    expect(displayedLogs).toEqual(expectedLogs);

    const logComparisons = expectedLogs.map((line, index) => ({
      lineNumber: index + 1,
      expected: line,
      displayed: displayedLogs[index],
      matches: displayedLogs[index] === line,
    }));

    await testInfo.attach('logs-comparison', {
      body: JSON.stringify(logComparisons, null, 2),
      contentType: 'application/json',
    });
  });
});

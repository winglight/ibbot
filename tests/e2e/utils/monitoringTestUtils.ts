import type { Page } from '@playwright/test';

type QueueSnapshot = {
  source: string;
  timestamp: string;
  queues: Array<{
    name: string;
    queue_key: string;
    length: number;
    consumption_rate: number;
    stale_seconds: number;
  }>;
  streams: Array<{
    name: string;
    stream_key: string;
    group: string;
    pending: number;
    consumption_rate: number;
    stale_seconds: number;
  }>;
};

type MetricsSeriesEntry = {
  time: string;
  value: number;
  service: string;
  metric: string;
};

type LogsResponse = {
  success: boolean;
  logs: string[];
  pagination: {
    total: number;
    page_size: number;
    direction: 'forward' | 'backward';
    start: number | null;
    end: number | null;
    has_more_backward: boolean;
    has_more_forward: boolean;
    next_backward_cursor: number | null;
    next_forward_cursor: number | null;
    tail_cursor: number | null;
  };
};

type MonitoringStubConfig = {
  queueSnapshot?: QueueSnapshot;
  metricsSeries?: MetricsSeriesEntry[];
  baseLogs?: string[];
  tailLogs?: string[];
  logModules?: string[];
};

export const queueSnapshot: QueueSnapshot = {
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

export const metricsSeries: MetricsSeriesEntry[] = [
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

export const baseLogs: string[] = [
  '| 2024-05-05 10:00:00 | INFO | queue_manager | REQUEST Q1 processed\n',
  '| 2024-05-05 10:00:05 | ERROR | queue_manager | REQUEST Q2 failed\n',
];

export const tailLogs: string[] = [
  '| 2024-05-05 10:00:10 | INFO | queue_manager | tail update delivered\n',
];

const computeMetricsSummary = (series: MetricsSeriesEntry[]) => {
  const summary: Record<string, { points: number; sum: number; average: number }> = {};
  series.forEach((entry) => {
    const key = entry.metric || entry.service || 'unknown';
    if (!summary[key]) {
      summary[key] = { points: 0, sum: 0, average: 0 };
    }
    summary[key].points += 1;
    summary[key].sum += entry.value;
  });

  Object.keys(summary).forEach((key) => {
    const stats = summary[key];
    stats.average = stats.points > 0 ? stats.sum / stats.points : 0;
  });

  return summary;
};

export async function stubMonitoringApis(page: Page, config: MonitoringStubConfig = {}) {
  const queueSnapshotData = config.queueSnapshot ?? queueSnapshot;
  const metricsSeriesData = config.metricsSeries ?? metricsSeries;
  const baseLogsData = config.baseLogs ?? baseLogs;
  const tailLogsData = config.tailLogs ?? tailLogs;
  const modules = config.logModules ?? ['queue_manager'];
  const metricsSummary = computeMetricsSummary(metricsSeriesData);

  let tailDelivered = false;

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const { pathname, searchParams } = url;

    if (pathname === '/api/monitoring/queues') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(queueSnapshotData),
      });
      return;
    }

    if (pathname === '/api/monitoring/metrics/write-rate') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          series: metricsSeriesData,
          summary: metricsSummary,
        }),
      });
      return;
    }

    if (pathname === '/api/logs/modules') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ modules }),
      });
      return;
    }

    if (pathname === '/api/logs') {
      const direction = searchParams.get('direction') ?? 'backward';
      const pageSize = Number(searchParams.get('page_size') || 200);

      if (direction === 'forward' && !tailDelivered && tailLogsData.length > 0) {
        tailDelivered = true;
        const total = baseLogsData.length + tailLogsData.length;
        const logsResponse: LogsResponse = {
          success: true,
          logs: tailLogsData,
          pagination: {
            total,
            page_size: pageSize,
            direction: 'forward',
            start: baseLogsData.length,
            end: total,
            has_more_backward: true,
            has_more_forward: false,
            next_backward_cursor: baseLogsData.length,
            next_forward_cursor: total,
            tail_cursor: total,
          },
        };
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(logsResponse),
        });
        return;
      }

      const total = baseLogsData.length + (tailDelivered ? tailLogsData.length : 0);
      const logsResponse: LogsResponse = {
        success: true,
        logs: baseLogsData,
        pagination: {
          total,
          page_size: pageSize,
          direction: 'backward',
          start: Math.max(0, total - baseLogsData.length),
          end: total,
          has_more_backward: false,
          has_more_forward: !tailDelivered && tailLogsData.length > 0,
          next_backward_cursor: null,
          next_forward_cursor: baseLogsData.length,
          tail_cursor: total,
        },
      };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(logsResponse),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ success: true }),
    });
  });
}

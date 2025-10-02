import { test, expect } from '@playwright/test';

type ConnectionStatusMeta = {
  connected: boolean;
  errorCode: number | null;
  lastError: string | null;
  redis?: { error_code: number | null; stale?: boolean } | null;
};

test.describe('connection status normalization', () => {
  test('captures backend error_code for disconnected state', async ({ page, baseURL }) => {
    await page.goto(`${baseURL}/static/index.html`);
    await page.waitForFunction(() => Boolean((window as any).app?.ibkrManager));

    const meta = await page.evaluate<ConnectionStatusMeta>(() => {
      const manager = (window as any).app?.ibkrManager;
      if (!manager) {
        throw new Error('IBKR manager not available');
      }
      manager.updateConnectionStatus(false, '', {
        source: 'redis',
        stale: true,
        last_error: 'TWS connection refused',
        error_code: '10147',
        queue_depth: '3',
        last_seen: '2024-05-05T10:00:00Z',
      });
      return manager.connectionStatusMeta;
    });

    expect(meta).toBeTruthy();
    expect(meta.connected).toBe(false);
    expect(meta.errorCode).toBe(10147);
    expect(meta.lastError).toBe('TWS connection refused');
    expect(meta.redis?.error_code).toBe(10147);
    expect(meta.redis?.stale).toBe(true);
  });
});

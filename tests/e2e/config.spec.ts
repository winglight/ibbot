import { test, expect } from '@playwright/test';

test.describe('Config center', () => {
  test('saves configuration and performs rollback', async ({ page, baseURL }) => {
    const calls: Array<{ type: string; body?: any }> = [];

    let currentValues: Record<string, string> = {
      STOP_LOSS_RATIO: '0.10',
      MAX_POSITION: '5',
    };

    let historyEntries = [
      {
        version: 'rev-1',
        module: 'risk',
        action: 'update',
        updated_by: 'tester',
        updated_at: '2024-05-01T00:00:00Z',
        message: 'initial baseline',
        values: { ...currentValues },
      },
    ];

    await page.route('**/api/config/modules', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ modules: ['risk'] }),
      });
    });

    await page.route('**/api/config/risk', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ module: 'risk', values: currentValues }),
        });
        return;
      }

      const payload = JSON.parse(route.request().postData() || '{}');
      calls.push({ type: 'save', body: payload });
      const newValues: Record<string, string> = {};
      for (const [key, value] of Object.entries(payload.values || {})) {
        if (value === null || value === undefined) continue;
        newValues[key] = String(value);
      }
      currentValues = newValues;
      historyEntries = [
        {
          version: 'rev-2',
          module: 'risk',
          action: 'update',
          updated_by: 'tester',
          updated_at: '2024-05-02T00:00:00Z',
          message: payload.message || 'update',
          values: { ...currentValues },
        },
        ...historyEntries,
      ];

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, module: 'risk', values: currentValues, version: 'rev-2' }),
      });
    });

    await page.route('**/api/config/risk/history', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ module: 'risk', entries: historyEntries }),
      });
    });

    await page.route('**/api/config/risk/rollback', async (route) => {
      const payload = JSON.parse(route.request().postData() || '{}');
      calls.push({ type: 'rollback', body: payload });
      const target = historyEntries.find((entry) => entry.version === payload.version);
      if (target) {
        currentValues = { ...target.values };
        historyEntries = [
          {
            version: 'rev-rollback',
            module: 'risk',
            action: 'rollback',
            updated_by: 'tester',
            updated_at: '2024-05-03T00:00:00Z',
            message: payload.message || 'rollback',
            restored_version: payload.version,
            values: { ...currentValues },
          },
          ...historyEntries,
        ];
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ success: true, module: 'risk', values: currentValues, version: 'rev-rollback' }),
      });
    });

    await page.goto(`${baseURL}/static/index.html`);
    await page.click('#openConfigurationBtn');
    await expect(page.locator('#configCenterTextarea')).toBeVisible();
    await expect(page.locator('#configCenterTextarea')).toContainText('STOP_LOSS_RATIO');

    await page.fill('#configCenterTextarea', '{\n  "STOP_LOSS_RATIO": 0.20\n}');
    await page.fill('#configCenterMessageInput', 'raise stop to 20%');
    await page.click('#configCenterSaveButton');
    await expect(page.locator('#configCenterStatusText')).toContainText('配置已保存');

    await page.click('button[data-version="rev-1"]');
    await expect(page.locator('#configCenterStatusText')).toContainText('回滚');
    await expect(page.locator('#configCenterTextarea')).toContainText('0.1');

    const saveCall = calls.find((entry) => entry.type === 'save');
    expect(saveCall?.body?.message).toBe('raise stop to 20%');
    expect(calls.some((entry) => entry.type === 'rollback')).toBeTruthy();
  });
});


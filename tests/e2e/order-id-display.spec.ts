import { test, expect } from '@playwright/test';

declare global {
  interface Window {
    testHarness: { app: any };
    appModules: any;
  }
}

test.describe('order id presentation', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/tests/e2e/fixtures/order_id_fixture.html');
  });

  test('order manager reconciliation keeps dataset ids and truncated text', async ({ page }) => {
    await page.evaluate(() => {
      const orders = [
        {
          order_id: 'ABC12345',
          symbol: 'AAPL',
          action: 'BUY',
          quantity: 10,
          order_type: 'MKT',
          avg_fill_price: 123.45,
          status: 'Submitted',
          submit_time: '2024-01-01T00:00:00Z',
          pnl: 0,
          commission: 0,
          order_source: 'manual',
        },
        {
          order_id: 'XYZ98765',
          symbol: 'MSFT',
          action: 'SELL',
          quantity: 5,
          order_type: 'LMT',
          limit_price: 310.12,
          status: 'PreSubmitted',
          submit_time: '2024-01-02T00:00:00Z',
          pnl: 0,
          commission: 0,
          order_source: 'strategy',
        },
      ];
      window.testHarness.app.orderManager.updateOrdersTableSmart(orders);
    });

    const rows = page.locator('#ordersTable tbody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toHaveAttribute('data-order-id', 'ABC12345');
    await expect(rows.nth(1)).toHaveAttribute('data-order-id', 'XYZ98765');
    await expect(rows.first().locator('.order-id-display')).toHaveText('345');
    await expect(rows.nth(1).locator('.order-id-display')).toHaveText('765');

    const texts = await page.$$eval('#ordersTable tbody .order-id-display', els => els.map(el => el.textContent?.trim() || ''));
    for (const text of texts) {
      expect(text).toHaveLength(3);
    }

    await page.evaluate(() => {
      const updated = [
        {
          order_id: 'XYZ98765',
          symbol: 'MSFT',
          action: 'SELL',
          quantity: 5,
          order_type: 'LMT',
          limit_price: 309.5,
          status: 'Filled',
          submit_time: '2024-01-02T00:00:00Z',
          pnl: 15.25,
          commission: 1.25,
          order_source: 'strategy',
        },
        {
          order_id: 'FOO00007',
          symbol: 'TSLA',
          action: 'BUY',
          quantity: 3,
          order_type: 'MKT',
          avg_fill_price: 210.11,
          status: 'Submitted',
          submit_time: '2024-01-03T00:00:00Z',
          pnl: 0,
          commission: 0,
          order_source: 'manual',
        },
      ];
      window.testHarness.app.orderManager.updateOrdersTableSmart(updated);
    });

    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toHaveAttribute('data-order-id', 'XYZ98765');
    await expect(rows.nth(1)).toHaveAttribute('data-order-id', 'FOO00007');
    await expect(rows.first().locator('.order-id-display')).toHaveText('765');
    await expect(rows.nth(1).locator('.order-id-display')).toHaveText('007');
  });

  test('formatOrderIdDisplay exposes tooltip with full value', async ({ page }) => {
    const result = await page.evaluate(() => {
      const display = window.testHarness.app.formatOrderIdDisplay('LONGORDERID');
      const container = document.createElement('div');
      container.innerHTML = display.html;
      const span = container.querySelector('.order-id-display');
      return {
        short: display.short,
        full: display.full,
        text: span?.textContent || '',
        title: span?.getAttribute('title') || '',
        aria: span?.getAttribute('aria-label') || '',
        data: span?.getAttribute('data-full-order-id') || '',
      };
    });

    expect(result.short).toBe('RID');
    expect(result.text).toBe('RID');
    expect(result.title).toBe('LONGORDERID');
    expect(result.aria).toBe('LONGORDERID');
    expect(result.data).toBe('LONGORDERID');
    expect(result.full).toBe('LONGORDERID');
  });

  test('risk rule orders render truncated ids', async ({ page }) => {
    await page.evaluate(async () => {
      const manager = new window.appModules.risk.manager.RiskRulesManager(window.testHarness.app);
      manager.fetchRiskRuleOrders = async () => ({
        totalOrders: 1,
        totalProfit: 0,
        totalLoss: 0,
        orders: [
          {
            order: {
              order_id: 'RISK9988',
              submit_time: '2024-01-01T00:00:00Z',
              created_at: '2024-01-01T00:00:00Z',
              symbol: 'TEST',
              action: 'BUY',
              quantity: 1,
              order_type: 'MKT',
              limit_price: null,
              avg_fill_price: 0,
              status: 'Filled',
              order_source: 'risk_rule',
            },
            orderPnl: 0,
          },
        ],
      });
      await manager.loadRiskRuleOrders('TEST');
    });

    const riskRow = page.locator('#riskRuleOrdersTable tbody tr').first();
    await expect(riskRow.locator('.order-id-display')).toHaveText('988');
    await expect(riskRow).toHaveAttribute('data-order-id', 'RISK9988');
  });
});

import { describe, expect, it } from 'vitest';

type StrategyRuntimeSummary = {
  strategy: string;
  is_receiving_data?: boolean | null;
  runtime_seconds?: number | null;
  processed_count?: number | null;
  stop_loss_enabled?: boolean | null;
  stop_loss_price?: number | null;
  take_profit_enabled?: boolean | null;
};

type RiskConfig = {
  strategy: string;
  stop_loss_enabled?: boolean | null;
  stop_loss_price?: number | null;
  take_profit_enabled?: boolean | null;
};

const formatRuntimeSummary = (summary: StrategyRuntimeSummary) => ({
  ...summary,
  is_receiving_data: summary.is_receiving_data ?? false,
  runtime_seconds: summary.runtime_seconds ?? 0,
  processed_count: summary.processed_count ?? 0,
});

const mergeWithRiskConfig = (
  summary: StrategyRuntimeSummary,
  risk: RiskConfig,
): StrategyRuntimeSummary => ({
  ...summary,
  stop_loss_enabled: risk.stop_loss_enabled ?? summary.stop_loss_enabled ?? null,
  stop_loss_price: risk.stop_loss_price ?? summary.stop_loss_price ?? null,
  take_profit_enabled:
    risk.take_profit_enabled ?? summary.take_profit_enabled ?? null,
});

describe('runtime metrics formatting', () => {
  it('coerces nullable runtime fields to default numeric values', () => {
    const formatted = formatRuntimeSummary({
      strategy: 'mean-reversion',
      is_receiving_data: true,
      runtime_seconds: 15,
      processed_count: 50,
    });

    expect(formatted.runtime_seconds).toBe(15);
    expect(typeof formatted.runtime_seconds).toBe('number');
    expect(formatted.processed_count).toBe(50);
    expect(typeof formatted.processed_count).toBe('number');
    expect(formatted.is_receiving_data).toBe(true);
  });

  it('preserves boolean risk flags when merging runtime state', () => {
    const runtime: StrategyRuntimeSummary = {
      strategy: 'momentum',
      is_receiving_data: false,
      stop_loss_enabled: true,
      stop_loss_price: 101.23,
      take_profit_enabled: false,
    };

    const merged = mergeWithRiskConfig(runtime, {
      strategy: 'momentum',
      stop_loss_enabled: true,
      stop_loss_price: 101.23,
      take_profit_enabled: false,
    });

    expect(merged.stop_loss_enabled).toBe(true);
    expect(typeof merged.stop_loss_enabled).toBe('boolean');
    expect(merged.stop_loss_price).toBe(101.23);
    expect(typeof merged.stop_loss_price).toBe('number');
    expect(merged.take_profit_enabled).toBe(false);
    expect(typeof merged.take_profit_enabled).toBe('boolean');
  });
});

export const seconds = (ms: number): string => `${(ms / 1000).toFixed(1)} s`;

export const usd = (amount: number | null): string =>
  amount === null ? "unknown" : `$${amount.toFixed(4)}`;

export const count = (value: number): string => value.toLocaleString("en-GB");

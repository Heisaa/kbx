import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

const color = (code, text) => `\x1b[${code}m${text}\x1b[0m`;
const dim = (text) => color('2', text);

let data = {};
try {
  data = JSON.parse(readFileSync(0, 'utf8'));
} catch {}

const cwd = data.workspace?.current_dir ?? data.cwd ?? '';

let branch = '';
try {
  branch = execFileSync('git', ['branch', '--show-current'], {
    cwd: cwd || undefined,
    stdio: ['ignore', 'pipe', 'ignore'],
    encoding: 'utf8',
  }).trim();
} catch {}

const ctx = Math.round(data.context_window?.used_percentage ?? 0);
const ctxColor = ctx >= 90 ? '31' : ctx >= 70 ? '33' : '32';

// rate_limits reports what has been consumed; the status line shows what is left.
// spend_limit.used_percentage can exceed 100, so clamp.
const left = (window) => {
  const used = data.rate_limits?.[window]?.used_percentage;
  return used == null ? null : Math.max(0, Math.round(100 - used));
};

const windows = [
  ['5h', left('five_hour')],
  ['7d', left('seven_day')],
  ['$', left('spend_limit')],
].filter(([, pct]) => pct !== null);

const limits = windows.length
  ? dim('left ') +
    windows
      .map(([label, pct]) =>
        `${dim(label)} ${color(pct <= 10 ? '31' : pct <= 25 ? '33' : '32', `${pct}%`)}`,
      )
      .join(dim(' · '))
  : '';

const parts = [
  color('35', `\u{1f4e6} ${process.env.SANDBOX_NAME ?? 'sandbox'}`),
  color('36', data.model?.display_name ?? '?'),
  color('34', cwd.split('/').pop() || '/'),
  branch && dim(`⎇ ${branch}`),
  `${dim('ctx')} ${color(ctxColor, `${ctx}%`)}`,
  limits,
].filter(Boolean);

console.log(parts.join(dim(' | ')));

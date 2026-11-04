// odj-snapshot-scheduler: triggers the options-data-journal `snapshot` workflow at the two daily slots.
// Cloudflare cron triggers are far more punctual than GitHub's own `schedule:` (which is best-effort and
// can skip runs entirely). Deployed as its own Worker so it never touches the user's `open-scheduler`.
//
// Secret (never committed): JOURNAL_ACTIONS_TOKEN = fine-grained GitHub PAT, ONLY this repo, Actions: Read and write.

const JOURNAL = "samedame/options-data-journal";

// Each slot's cron lists both UTC offsets (EDT/EST) in ONE expression, because the Workers Free plan allows only
// 5 cron triggers per account and `open-scheduler` already uses 3. The New York window check drops the wrong hour.
const SLOT_BY_CRON = {
  "45 13,14 * * 1-5": "open", // 09:45 EDT / 09:45 EST
  "50 19,20 * * 1-5": "close", // 15:50 EDT / 15:50 EST
};

// Minutes since midnight in New York, tight around each nominal slot (09:45 / 15:50) so the cron at the wrong
// UTC offset (08:45/10:45 or 14:50/16:50 ET) never dispatches. NOTE: no cron fires at 12:50 ET, so the
// half-day close slot (12:50) is not triggered by this Worker; see DECISIONS.md.
const WINDOW = { open: [9 * 60 + 40, 10 * 60 + 30], close: [15 * 60 + 45, 16 * 60 + 30] };

export function nyMinutes(date = new Date()) {
  const p = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  })
    .formatToParts(date)
    .reduce((a, x) => ((a[x.type] = x.value), a), {});
  return Number(p.hour) * 60 + Number(p.minute);
}

export function inWindow(slot, date = new Date()) {
  const w = WINDOW[slot];
  if (!w) return false;
  const now = nyMinutes(date);
  return now >= w[0] && now <= w[1];
}

export async function dispatchSnapshot(env, slot, date = new Date()) {
  if (!inWindow(slot, date)) return { skipped: "outside New York slot window" };
  if (!env.JOURNAL_ACTIONS_TOKEN) {
    console.error("JOURNAL_ACTIONS_TOKEN is not set; cannot dispatch the snapshot workflow");
    return { error: "missing token" };
  }
  const r = await fetch(`https://api.github.com/repos/${JOURNAL}/actions/workflows/snapshot.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.JOURNAL_ACTIONS_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "odj-snapshot-scheduler",
    },
    body: JSON.stringify({ ref: "main", inputs: { slot } }),
  });
  if (r.status !== 204) {
    const body = await r.text();
    console.error("journal dispatch failed", r.status, body);
    return { error: `github ${r.status}` };
  }
  return { dispatched: slot };
}

export default {
  async scheduled(event, env, ctx) {
    const slot = SLOT_BY_CRON[event.cron];
    if (slot) ctx.waitUntil(dispatchSnapshot(env, slot));
    else console.error("unknown cron expression", event.cron);
  },
};

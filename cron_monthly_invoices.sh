#!/usr/bin/env bash
# Monthly Vetmarket invoice sync — pulls order-confirmation PDFs from Yahoo,
# parses line items, updates data/vetmarket.db (prices/invoices/invoice_lines).
# Dashboard reads the DB live, so no service restart is needed for data.
# Installed in claude-user crontab: 0 6 6 * *  (6th of each month, 06:00).
set -euo pipefail

PROJ="/home/claude-user/projects/vetmarket-cli"
LOG="$PROJ/data/monthly_sync.log"
PY="/usr/bin/python3"
STAMP="$(date -Is)"

# WhatsApp alert to Gil on failure/anomaly (reuses the shared notify helper).
NOTIFY="$HOME/brain-dreamer/notify.sh"
wa_alert() {
  [ -x "$NOTIFY" ] && bash "$NOTIFY" "$1" >>"$LOG" 2>&1 || \
    echo "WARN: notify helper missing, alert not sent: $1" >>"$LOG"
}

cd "$PROJ"
echo "==== $STAMP  monthly invoice sync START ====" >>"$LOG"

# Backup DB before touching it (additive, but be safe per deploy rules).
cp -f data/vetmarket.db "data/vetmarket.db.bak-$(date +%Y%m%d-%H%M)" 2>>"$LOG" || \
  echo "WARN: db backup failed" >>"$LOG"

# Keep only the 10 most recent backups (|| true: empty glob must not abort set -e).
ls -1t data/vetmarket.db.bak-* 2>/dev/null | tail -n +11 | xargs -r rm -f || true

# Sync. since_days=120 overlaps a month or two to catch late/Trash invoices;
# yahoo_invoices dedups by order_no so re-pulls are harmless.
if RESULT=$("$PY" -c "from vetmarket import yahoo_invoices as yi; import json; print(json.dumps(yi.sync(since_days=120)))" 2>>"$LOG"); then
  echo "RESULT: $RESULT" >>"$LOG"
  # Detect a silent no-op (0 new invoices) — sync "succeeded" but pulled nothing.
  NEW=$(printf '%s' "$RESULT" | "$PY" -c "import sys,json;
try:
    d=json.load(sys.stdin); print(d.get('new_invoices', d.get('new', d.get('inserted', -1))))
except Exception:
    print(-1)" 2>>"$LOG" || echo -1)
  if [ "$NEW" = "0" ]; then
    wa_alert "⚠️ וטמרקט: סנכרון חשבוניות חודשי רץ אך לא נמצאו חשבוניות חדשות ($STAMP). ייתכן שהמחירון מתיישן — כדאי לבדוק את תיבת Yahoo."
  fi
else
  echo "ERROR: sync failed (see traceback above)" >>"$LOG"
  echo "==== $STAMP  END (FAILED) ====" >>"$LOG"
  wa_alert "🔴 וטמרקט: סנכרון חשבוניות חודשי נכשל ($STAMP). הדשבורד מציג מחירון ישן. ראה $LOG בשרת."
  exit 1
fi

# Quick post-sync sanity count.
"$PY" -c "from vetmarket import reports; s=reports.status_summary(); print('SUMMARY: invoices=%s lines=%s prices=%s' % (s['invoices'], s['invoice_lines'], s['price_observations']))" >>"$LOG" 2>&1 || true

echo "==== $STAMP  monthly invoice sync DONE ====" >>"$LOG"

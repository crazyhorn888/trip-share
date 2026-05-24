#!/bin/bash
# trip-sync/sync.sh
# launchd 觸發後執行此腳本，記錄 log

SCRIPTS_DIR="$HOME/Desktop/Mine/9. Coding/Claude Code/100_Todo/projects/trip-share/scripts"
LOG="$SCRIPTS_DIR/sync.log"

echo "" >> "$LOG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >> "$LOG"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 偵測到檔案變動，開始同步" >> "$LOG"

/usr/bin/python3 "$SCRIPTS_DIR/parse_numbers.py" >> "$LOG" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 同步完成 ✅" >> "$LOG"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 同步失敗 ❌ (exit $EXIT_CODE)" >> "$LOG"
fi

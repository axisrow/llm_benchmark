#!/usr/bin/env bash
# Перепрогон ошибочных моделей: по одной, по 5 копий, пока все не пройдут.
set -euo pipefail

DB="data/main.db"
MAX_RETRIES=3

# SQL: уникальные ошибочные комбинации минус denylist
COMBOS=$(sqlite3 "$DB" "
  SELECT DISTINCT r.project, r.provider, r.model
  FROM reports r, json_each(json_extract(r.raw_json, '\$.runs')) as run
  WHERE json_extract(run.value, '\$.code') != 0
    AND NOT EXISTS (
      SELECT 1 FROM model_exclusions me
      WHERE me.active = 1
        AND me.provider = r.provider
        AND me.model = r.model
    )
  ORDER BY r.project, r.provider, r.model
")

if [ -z "$COMBOS" ]; then
  echo "Нет ошибочных комбинаций для перезапуска."
  exit 0
fi

TOTAL=$(echo "$COMBOS" | wc -l | tr -d ' ')
echo "Найдено комбинаций для перезапуска: $TOTAL"
echo ""

FAILED=()
CURRENT=0
while IFS='|' read -r project provider model; do
  CURRENT=$((CURRENT + 1))
  # Prompt из projects_library (экранируем одинарные кавычки)
  prompt=$(sqlite3 "$DB" "SELECT prompt FROM projects_library WHERE name = '$project'")

  attempt=0
  ok=false
  while [ $attempt -lt $MAX_RETRIES ] && [ "$ok" = false ]; do
    attempt=$((attempt + 1))
    echo "=== [$CURRENT/$TOTAL] попытка $attempt/$MAX_RETRIES: $project | $provider/$model ==="
    if python bench.py --project "$project" -p "$provider" -m "$model" -n 5 "$prompt"; then
      ok=true
      echo "  ✓ Успешно"
    else
      echo "  ✗ Ошибка (код $?)"
    fi
  done

  if [ "$ok" = false ]; then
    FAILED+=("$provider/$model ($project)")
  fi
  echo ""
done <<< "$COMBOS"

echo "================================"
echo "Итого: $CURRENT комбинаций, ${#FAILED[@]} не прошли."
if [ ${#FAILED[@]} -gt 0 ]; then
  echo ""
  echo "НЕ прошли после $MAX_RETRIES попыток:"
  printf '  - %s\n' "${FAILED[@]}"
  exit 1
else
  echo "Все модели прошли успешно!"
fi

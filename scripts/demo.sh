#!/usr/bin/env bash
# Прогон сценариев каскада против эмулятора. Требует: запущенные api, worker, emulator.
#   API=http://localhost:18000 EMU=http://localhost:18001 ./scripts/demo.sh
set -euo pipefail

API=${API:-http://localhost:18000}
EMU=${EMU:-http://localhost:18001}
RUN=$(date +%s)
FAILS=0

pay() {  # pay <name> <json>
  curl -s -X POST "$API/v1/payments" -H 'Content-Type: application/json' \
    -H "Idempotency-Key: demo-$RUN-$1" -d "$2"
}
card() {  # card <pan> [amount]
  printf '{"flow":"card","order_id":"order-%s","amount":"%s","description":"demo","card":{"pan":"%s","expiry":"12/29","cvc":"123"}}' \
    "$RANDOM" "${2:-1500.00}" "$1"
}
link() {  # link <amount> [method]
  printf '{"flow":"link","order_id":"order-%s","amount":"%s","description":"demo","method":"%s"}' \
    "$RANDOM" "$1" "${2:-sbp}"
}
get() { curl -s "$API/v1/payments/$1"; }
field() { python3 -c "import json,sys; p=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

show() { python3 "$(dirname "$0")/show_payment.py"; }

expect() {  # expect <payment_json> <status> <attempts> [title]
  local got_status got_n
  got_status=$(echo "$1" | field 'p["status"]')
  got_n=$(echo "$1" | field 'len(p["attempts"])')
  if [[ "$got_status" == "$2" && "$got_n" == "$3" ]]; then
    echo "   ✔ ok"
  else
    echo "   ✘ expected status=$2 attempts=$3, got status=$got_status attempts=$got_n"
    FAILS=$((FAILS + 1))
  fi
}

wait_status() {  # wait_status <payment_id> <status> [timeout_s]
  local deadline=$(( $(date +%s) + ${3:-30} ))
  while (( $(date +%s) < deadline )); do
    [[ "$(get "$1" | field 'p["status"]')" == "$2" ]] && return 0
    sleep 1
  done
}

section() { printf '\n\033[1m%s\033[0m\n' "$1"; }

curl -sf "$API/health" >/dev/null || { echo "API $API is not reachable"; exit 1; }
reset_all() {
  for p in paymaster_a paymaster_b pally; do
    curl -s -X POST "$API/v1/providers/$p/circuit/reset" >/dev/null
  done
  for p in paymaster-a paymaster-b pally; do
    curl -s -X POST "$EMU/$p/_admin/outage?seconds=0" >/dev/null
  done
}
reset_all

section "CARD 1. Мягкий отказ на эквайере A → успех на B"
c1_body=$(card 4000000000000002)
r=$(pay c1 "$c1_body"); echo "$r" | show; expect "$r" succeeded 2

section "CARD 2. Жёсткий отказ (InsufficientFunds) → стоп, к B не идём"
r=$(pay c2 "$(card 4000000000009995)"); echo "$r" | show; expect "$r" failed 1

section "CARD 3. Фрод (RejectedByFraud) → стоп"
r=$(pay c3 "$(card 4000000000000127)"); echo "$r" | show; expect "$r" failed 1

section "CARD 4. 500 на A → B"
r=$(pay c4 "$(card 4000000000000119)"); echo "$r" | show; expect "$r" succeeded 2

section "CARD 5. Мягкий отказ на A и B → hosted-резерв Pally (переадресация)"
r=$(pay c5 "$(card 4000000000000341)"); echo "$r" | show; expect "$r" requires_action 3
bill_url=$(echo "$r" | field 'p["next_action"]["url"]')
bill_id=${bill_url##*/}
echo "   клиент платит на странице Pally…"
curl -s -o /dev/null -X POST "$EMU/pally/transfer/$bill_id/pay" -d result=success
pid=$(echo "$r" | field 'p["id"]'); wait_status "$pid" succeeded 15
r=$(get "$pid"); echo "$r" | show; expect "$r" succeeded 3

section "CARD 6. 3DS → подтверждение → успех"
r=$(pay c6 "$(card 4000000000003220)"); echo "$r" | show; expect "$r" requires_action 1
attempt=$(echo "$r" | field 'p["attempts"][-1]["id"]')
echo "   клиент проходит 3DS (ACS → POST на наш return URL)…"
r=$(curl -s -X POST "$API/v1/return/$attempt" -d PaRes=ok -d MD=x); echo "$r" | show; expect "$r" succeeded 1

section "CARD 7. 3DS не пройден → HARD, стоп"
r=$(pay c7 "$(card 4000000000003220)")
attempt=$(echo "$r" | field 'p["attempts"][-1]["id"]')
r=$(curl -s -X POST "$API/v1/return/$attempt" -d PaRes=fail -d MD=x); echo "$r" | show; expect "$r" failed 1

section "CARD 8. Таймаут на A, а деньги списались → выясняем статус, второй раз НЕ списываем"
r=$(pay c8 "$(card 4000000000000267)"); echo "$r" | show; expect "$r" succeeded 1

section "CARD 9. Таймаут на A, платёж не создан → выясняем статус → B"
r=$(pay c9 "$(card 4000000000000259)"); echo "$r" | show; expect "$r" succeeded 2

section "CARD 10. Повтор запроса с тем же Idempotency-Key → тот же платёж, без нового списания"
r=$(pay c1 "$c1_body"); echo "$r" | show; expect "$r" succeeded 2
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/payments" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: demo-$RUN-c1" -d "$(card 4111111111111111 99.00)")
echo "   тот же ключ, другое тело → HTTP $code"; [[ $code == 409 ]] && echo "   ✔ ok" || FAILS=$((FAILS + 1))

section "CARD 11. Падение эквайера A → circuit breaker → сразу B"
curl -s -X POST "$EMU/paymaster-a/_admin/outage?seconds=60" >/dev/null
for i in 1 2 3; do pay "cb-$i" "$(card 4111111111111111)" >/dev/null; done
r=$(pay c11 "$(card 4111111111111111)"); echo "$r" | show; expect "$r" succeeded 1
first=$(echo "$r" | field 'p["attempts"][0]["provider"]')
[[ $first == paymaster_b ]] && echo "   ✔ A пропущен без попытки" || { echo "   ✘ first attempt went to $first"; FAILS=$((FAILS + 1)); }
curl -s "$API/v1/providers" | python3 -c 'import json,sys; [print("   circuit", p["provider"], "OPEN" if p["circuit_open"] else "closed") for p in json.load(sys.stdin) if p["flow"]=="card"]'
reset_all

section "LINK 1. Pally выдал ссылку с первого раза"
r=$(pay l1 "$(link 1000.00)"); echo "$r" | show; expect "$r" requires_action 1

section "LINK 2. Pally: «направление недоступно» (SOFT) → ссылка PayMaster"
r=$(pay l2 "$(link 1000.01)"); echo "$r" | show; expect "$r" requires_action 2

section "LINK 3. Pally не ответил (таймаут) → ссылка PayMaster, осиротевший счёт Pally гасим"
r=$(pay l3 "$(link 1000.02)"); echo "$r" | show; expect "$r" requires_action 2
pid=$(echo "$r" | field 'p["id"]'); sleep 6
r=$(get "$pid"); echo "$r" | show
[[ "$(echo "$r" | field 'p["attempts"][0]["status"]')" == "cancelled" ]] && echo "   ✔ orphan cancelled" \
  || { echo "   ✘ orphan not cancelled"; FAILS=$((FAILS + 1)); }

section "LINK 4. Pally: 401 (CONFIG) → алерт, ссылка PayMaster"
r=$(pay l4 "$(link 1000.03)"); echo "$r" | show; expect "$r" requires_action 2

section "LINK 5. Pally: invalid_amount (HARD) → сразу ошибка клиенту"
r=$(pay l5 "$(link 1000.04)"); echo "$r" | show; expect "$r" failed 1

section "LINK 6. Клиент оплатил по ссылке → postback → успех"
r=$(pay l6 "$(link 2500.00)"); bill_id=$(echo "$r" | field 'p["attempts"][0]["provider_payment_id"]')
curl -s -o /dev/null -X POST "$EMU/pally/transfer/$bill_id/pay" -d result=success
pid=$(echo "$r" | field 'p["id"]'); wait_status "$pid" succeeded 15
r=$(get "$pid"); echo "$r" | show; expect "$r" succeeded 1

section "LINK 7. Отказ на странице оплаты → не каскадим (отказ эмитента)"
r=$(pay l7 "$(link 2600.00)"); bill_id=$(echo "$r" | field 'p["attempts"][0]["provider_payment_id"]')
curl -s -o /dev/null -X POST "$EMU/pally/transfer/$bill_id/pay" -d result=fail
pid=$(echo "$r" | field 'p["id"]'); wait_status "$pid" failed 15
r=$(get "$pid"); echo "$r" | show; expect "$r" failed 1

section "WEBHOOK. Поддельная подпись Pally → 403"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/webhooks/pally" \
  -d InvId=x -d OutSum=1.00 -d TrsId=x -d Status=SUCCESS -d SignatureValue=DEADBEEF)
echo "   HTTP $code"; [[ $code == 403 ]] && echo "   ✔ ok" || FAILS=$((FAILS + 1))

echo
if (( FAILS )); then echo "FAILED: $FAILS"; exit 1; else echo "ALL SCENARIOS PASSED"; fi

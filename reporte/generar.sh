#!/usr/bin/env bash
# Reporte de BTC escrito por Claude a partir de data/reporte/contexto.json.
#
# Uso:
#   reporte/generar.sh                 reporte diario
#   reporte/generar.sh --si-hay-orden  solo si la estrategia o la cuenta paper C tienen una orden
#                                      para la próxima apertura (una alerta por vela de 4 h)
#
# Salida: data/reporte/<fecha UTC>_<diario|alerta-buy|alerta-sell|alerta-paper>.md y una copia en ultimo.md.
# Variables opcionales: REPORTE_MODEL (default sonnet), CLAUDE_BIN.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=data/reporte
MODEL="${REPORTE_MODEL:-sonnet}"
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
PY=.venv/bin/python
mkdir -p "$OUT"

state() { "$PY" -c "import json; print(json.load(open('data/estrategia/state.json'))['$1'] or '')"; }
# Exposición objetivo de la orden pendiente de la cuenta paper C (vacío si no hay o no existe paper.json)
orden_paper() {
    "$PY" -c "import json; o = json.load(open('data/estrategia/paper.json'))['accounts']['C'].get('order'); print('' if o is None else o['target_exposure'])" 2>/dev/null || true
}

tipo=diario
pedido="Reporte diario."
if [[ "${1:-}" == "--si-hay-orden" ]]; then
    accion=$(state action_next_open)
    paper=$(orden_paper)
    [[ -z "$accion" && -z "$paper" ]] && exit 0
    vela=$(state last_candle_open_utc)
    [[ -f "$OUT/.ultima_alerta" && "$(cat "$OUT/.ultima_alerta")" == "$vela" ]] && exit 0
    if [[ -n "$accion" ]]; then
        tipo="alerta-$accion"
        pedido="ALERTA: la estrategia tiene una orden '$accion' para la apertura de la próxima vela de 4 h."
    else
        tipo="alerta-paper"
        pedido="ALERTA (paper trading): la cuenta simulada C pasa a exposición $paper en la apertura de la próxima vela de 4 h. Usá el título '# 🔔 BTC — paper C a ${paper}x' en vez del de compra/venta."
    fi
fi

"$PY" reporte/contexto.py > /dev/null
dest="$OUT/$(date -u +%Y-%m-%d_%H%M)_$tipo.md"

# Sin herramientas: Claude solo lee el JSON del mensaje y escribe el texto.
timeout 600 "$CLAUDE" -p --model "$MODEL" --tools "" --no-session-persistence \
    --system-prompt "$(cat reporte/prompt.md)" \
    "$pedido

\`\`\`json
$(cat "$OUT/contexto.json")
\`\`\`" < /dev/null > "$dest.tmp"

[[ -s "$dest.tmp" ]] || { echo "Claude no devolvió texto" >&2; rm -f "$dest.tmp"; exit 1; }
mv "$dest.tmp" "$dest"
cp "$dest" "$OUT/ultimo.md"
[[ "$tipo" != diario ]] && echo "$vela" > "$OUT/.ultima_alerta"
echo "$dest"

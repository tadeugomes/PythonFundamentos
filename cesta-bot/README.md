# cesta-bot

Bot de cesta de ações da B3 com decisão semanal, backtest contra Ibovespa e CDI, sinais no Telegram e execução opcional no MetaTrader 5 da XP.

## Como funciona

Toda sexta (ou segunda antes da abertura) o bot:

1. Baixa 18 meses de preços do Yahoo Finance para o universo em `config.yaml`.
2. Calcula momentum 12-1, SMA50 e volatilidade de cada ativo. Exclui ROE negativo e alavancagem alta (fundamentos via yfinance, cache mensal).
3. Verifica o regime: Ibovespa abaixo da SMA200 reduz a exposição para 50%.
4. Vende posições que acionaram o stop móvel (12% do pico), caíram abaixo da SMA50 ou saíram do top 2n de momentum. Compra as melhores ranqueadas até completar a cesta (8 ativos).
5. Registra a carteira em `state/portfolio.json`, envia o resumo no Telegram e, no modo `mt5`, envia as ordens.

O backtest usa só fatores de preço porque não há histórico gratuito confiável de fundamentos. O filtro fundamentalista atua apenas ao vivo.

## Instalação

```bash
pip install -r requirements.txt
python backtest.py            # ~2 min na primeira vez (baixa desde 2013)
```

Saída em `output/backtest.csv`, `output/trades.csv` e `output/backtest.png`. Rode variações: `python backtest.py --start 2020-01-01`. Mude parâmetros em `config.yaml` e compare. Desconfie de qualquer combinação que só funciona com um conjunto estreito de valores.

## Modos de execução

Defina `execution.mode` em `config.yaml`:

| modo | o que faz |
|---|---|
| `paper` | carteira simulada, só imprime no terminal |
| `telegram` | carteira simulada + mensagem no Telegram |
| `mt5` | envia ordens pelo MetaTrader 5 (com `dry_run: true` só simula) |

Ordem recomendada: `paper` por duas semanas para validar o fluxo, `telegram` por dois ou três meses, depois `mt5` com `dry_run: true` por duas semanas, e só então `dry_run: false` com `capital_per_position` baixo (R$ 500 por ativo usa o mercado fracionário).

## Telegram

1. No Telegram, fale com `@BotFather`, envie `/newbot` e guarde o token.
2. Envie qualquer mensagem ao seu bot, depois abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id`.
3. Defina as variáveis de ambiente `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.

## MetaTrader 5 na XP

1. No portal XP: Minha Conta > Ferramentas e Serviços > Assinaturas de plataformas > MetaTrader 5. Peça a conta demo primeiro (servidor `XPMT5-DEMO`).
2. Instale o terminal MT5 no Windows (ou VPS Windows), faça login uma vez manualmente e habilite "Permitir trading algorítmico" em Ferramentas > Opções > Expert Advisors.
3. `pip install MetaTrader5` e defina `MT5_LOGIN` e `MT5_PASSWORD`. Ajuste `mt5.server` e `mt5.terminal_path` no config.
4. Teste: `python run_weekly.py --mode mt5` com `dry_run: true`.

Limitações conhecidas da XP: o MT5 é unicast, então posições abertas pelo home broker não aparecem nele. Mantenha a cesta inteira dentro do MT5 e não misture com operações manuais na mesma conta, ou use a sub-conta demo para o teste. O fracionário no MT5 pode exigir habilitação; se `PETR4F` não aparecer no Observatório de Mercado, peça à XP ou suba `capital_per_position` para cobrir lotes de 100.

## Agendamento no VPS Windows

Agendador de Tarefas: criar tarefa semanal, sexta 18:30, ação `python C:\cesta-bot\run_weekly.py`, "Iniciar em" `C:\cesta-bot`. Marque "Executar estando o usuário conectado ou não" para o MT5 abrir em sessão.

Para só sinais (sem MT5), `run_weekly.py --mode telegram` roda em qualquer Linux barato ou no GitHub Actions com `cron: "30 21 * * 5"` e os dois segredos do Telegram.

## Estrutura

```
config.yaml      universo, parâmetros, custos, modo
data.py          preços (Yahoo), Selic/CDI (BCB), fundamentos (yfinance)
strategy.py      regras de entrada, saída e regime
backtest.py      simulação semanal com custos
run_weekly.py    rodada ao vivo
broker_mt5.py    ordens no MetaTrader 5
notify.py        Telegram
state/           carteira registrada
output/          resultados do backtest
```

## Aviso

Nada aqui é recomendação de investimento. Momentum funciona em média e falha em reversões bruscas, com drawdowns de 30% ou mais em anos como 2015, 2020 e 2021. O stop móvel limita perdas por ativo, não da cesta. Use capital que pode perder durante o teste.

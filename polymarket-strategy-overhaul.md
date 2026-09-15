# Polymarket Trading Bot: Strategic Overhaul

**Подготовлено для:** Leonid  
**Дата:** 12 апреля 2026  
**Текущий bankroll:** $80 (paper mode)

---

## Executive Summary

Твой бот прошёл классический путь: начальная удача на CRYPTO_5m ($400 → $2000) оказалась variance, а не edge. Стратегия с 45% WR — это не просто "ниже монетки", это гарантированный слив с учётом 1.8% taker fee. Главный вывод из исследования: **успешные боты на Polymarket зарабатывают не на предсказании направления крипто за 5 минут, а на информационном edge — погода, threshold markets, арбитраж.**

**Приоритеты (от высшего к низшему):**

1. **ОТКЛЮЧИТЬ CRYPTO_5m** — немедленно, это чёрная дыра
2. **Масштабировать THRESHOLD** — единственная прибыльная стратегия, 100% WR
3. **Перестроить WEATHER** — доказанный edge, $24K и $65K реальные кейсы
4. **Market Making** — пассивный доход, 3x бонус за ликвидность
5. **LLM Pipeline 2.0** — после стабилизации первых трёх

---

## 1. Анализ реальных кейсов

### Подтверждённые истории успеха

**$313 → $438,000 за месяц** — бот торговал BTC/ETH/SOL **15-минутные** (не 5-минутные!) up/down рынки с 98% WR. Ключевая стратегия: ставки за 10 секунд до закрытия рынка, когда цена на Polymarket отстаёт от Binance/Coinbase spot. Это именно то, что делает твой LASTSEC, но у того бота был капитал для масштабирования.

**$1,000 → $24,000** — weather bot, торгует только London markets с апреля 2025. Стратегия: сравнивает NOAA/GFS прогнозы с ценами на Polymarket, ставит когда расхождение > 5%.

**$65,000 total profit** — weather bot на NYC, London, Seoul. Диверсификация по городам.

**$2,000,000+ за 2 месяца** — ensemble probability models + news analysis. Капитализация на mispricing.

**"Bidou28old"** — $116,280 за один день, 52 трейда, 83% WR.

**"gopfan2"** — $2M cumulative на weather. Правила: покупай YES < $0.15, NO > $0.45, max $1 risk per trade.

**Статистика:** 14 из 20 самых прибыльных кошельков на Polymarket — это боты. Но только 0.51% всех кошельков когда-либо заработали > $1,000.

### GitHub-репозитории для изучения

- `warproxxx/poly-maker` — market making bot (автор сам говорит "not profitable as-is")
- `aulekator/Polymarket-BTC-15-Minute-Trading-Bot` — BTC prediction
- `suislanchez/polymarket-kalshi-weather-bot` — GFS ensembles + Kelly, reported $1.8K profit
- `alteregoeth-ai/weatherbot` — Kelly Criterion + EV filtering
- `hcharper/polyBot-Weather` — edge thresholds + Kelly caps

---

## 2. Стратегическая переработка

### 2.1 CRYPTO_5m — ОТКЛЮЧИТЬ

**Решение:** Полностью отключить. Не чинить.

**Почему:**
- 45% WR хуже монетки. С 1.8% taker fee break-even на 50/50 рынке = 52%+ WR. Ты на 7% ниже break-even.
- RSI — это lagging oscillator. На 5-минутном таймфрейме с 14-period RSI ты анализируешь 70+ минут истории для предсказания 5-минутного окна. Это фундаментально не работает.
- Polymarket **ввёл dynamic fees** специально чтобы убить latency arbitrage на коротких крипто-рынках. Ранние боты извлекли ~$40M, но это окно закрыто.
- Твоя latency 700ms из NYC — не конкурентное преимущество. Топовые боты работают на 4ms из Tokyo.
- Академический консенсус: ML модели (LSTM, XGBoost) показывают 79-82% F1 на бэктестах, но в live торговле деградируют до coin-flip из-за market impact.

**Приоритет:** HIGH  
**Ожидаемый эффект:** Остановка потерь $1,282+ (основной drain)  
**Сложность:** 1/5 (просто выключить)

**Если всё-таки хочешь крипто-рынки**, переключись на **15-минутные**, используй LASTSEC-подход (ставка за 10 секунд до закрытия) с увеличенным размером ставки. Бот $313→$438K работал именно так.

---

### 2.2 THRESHOLD — Масштабировать агрессивно

**Решение:** Сделать основной стратегией. Расширить на ETH, SOL, XRP.

**Конкретные шаги:**

1. **Скрейпить ВСЕ threshold рынки через Gamma API** — не только BTC. Polymarket имеет 1,904+ активных ETH рынка, плюс SOL, XRP. Фильтр: `"Will [CRYPTO] be above $X on [date]?"`.

2. **Term structure arbitrage** — когда "BTC above $100K Sept 20" = 46% и "BTC above $100K Nov 13" = 48%, кривая неоправданно плоская для 55-дневного разрыва. Покупай дальний, продавай ближний.

3. **Автоматический скрининг** — для каждого threshold рынка:
   - Получи текущую цену BTC с Binance
   - Рассчитай distance to strike (текущая цена vs threshold)
   - Рассчитай время до экспирации
   - Используй implied volatility для оценки true probability
   - Сравни с рыночной ценой → если edge > 5%, ставь

4. **Количество рынков:** С ETH + SOL + XRP threshold рынками можно выйти на 20-50 трейдов в день вместо 4.

**Приоритет:** HIGH  
**Ожидаемый эффект:** При сохранении 90%+ WR и 20+ трейдов/день → $50-200/день при $400 bankroll  
**Сложность:** 2/5 (расширение существующей логики)

---

### 2.3 WEATHER — Перестроить правильно

**Решение:** Полная переработка на multi-model consensus.

**Почему потеряли $2,040:** Ставки на exact temperature buckets. Погодные модели дают ±2-3°C uncertainty — exact bucket = лотерея.

**Правильная стратегия (из реальных кейсов):**

1. **Только above/below endpoints** — это уже реализовано, правильно
2. **Multi-model consensus:**
   - GFS (31 ensemble, бесплатно через Open-Meteo) — уже используете
   - ECMWF (51 ensemble, 9km resolution) — добавить через Open-Meteo API (бесплатно)
   - ICON (DWD) — третья модель для consensus
   - **Правило: ставь только когда 3+ модели согласны**, implied probability 70-90%

3. **Timing advantage:** Модели обновляются каждые 6 часов. Рынки лагают на минуты-часы. Бот должен автоматически проверять обновления моделей и ставить сразу.

4. **Города по прибыльности (из кейсов):**
   - London — самый прибыльный ($24K на одном London)
   - NYC — второй
   - Seoul — третий
   - Причина: больше ликвидности + больше "казуальных" bettor'ов

5. **Sizing:** Max $50/ставку (уже установлено) — правильно для $400 bankroll. При росте до $2K поднять до $100-150.

6. **Edge threshold:** Минимум 5% расхождение между моделью и рынком. Тайтовее = больше noise.

7. **Правило gopfan2 ($2M trader):** Покупай YES < $0.15, NO > $0.45, max $1 risk per trade. Простое, но работает.

**Приоритет:** HIGH  
**Ожидаемый эффект:** $500-2,000/месяц при $400 bankroll (на основе реальных кейсов)  
**Сложность:** 3/5 (добавление ECMWF + ICON, consensus logic)

---

### 2.4 Market Making

**Решение:** Реализовать, но только после bankroll > $2,000.

**Как работает 3x бонус:** Polymarket даёт rewards за ликвидность. Двусторонние ордера (bid + ask) получают ~3x множитель vs односторонние. При extreme probabilities (< 0.10 или > 0.90) односторонние ордера получают **ноль** rewards.

**Стратегия:**
- Ставь bid + ask на обе стороны рынка
- Spread = 2-5% (YES bid 0.47 + YES ask 0.53)
- Inventory management: если набрал слишком много YES, сдвинь ask вниз
- Stoikov model для adaptive spread

**Реальность:** Создатель poly-maker (GitHub) прямо говорит "this bot is not profitable and will lose money" в текущем конкурентном окружении. MM на Polymarket — это скорее **доход от rewards** (как вознаграждение за ликвидность), а не от spread.

**Минимальный капитал:** $2,000+ для покрытия обеих сторон нескольких рынков одновременно.

**Приоритет:** MEDIUM  
**Ожидаемый эффект:** $100-500/месяц от rewards при $2K+ bankroll  
**Сложность:** 4/5 (inventory management, dynamic spreads)

---

### 2.5 Copy Trading 2.0

**Решение:** Полностью перестроить фильтрацию.

**Почему сливает (-$2,787):** Avg loss $122 vs avg win $53 = reward/risk 0.43x. Ты копируешь ВСЕ сделки китов, включая их хеджи и спекуляции.

**Правильный подход:**
1. **Фильтр по типу рынка:** Копируй ТОЛЬКО weather + threshold trades от проверенных кошельков
2. **Фильтр по размеру:** Копируй только крупные ставки (> $500 от кита) — это их conviction trades
3. **Фильтр по WR кита:** Минимум 60% WR за последние 30 дней на конкретном типе рынка
4. **Фильтр по скорости:** Не копируй если прошло > 2 минуты (цена уже сдвинулась)
5. **Обратное копирование:** Если кит стабильно сливает на каком-то типе рынка — ставь против него

**Инструменты:** PolyCop, Polygun, Kreo для отслеживания кошельков.

**Приоритет:** LOW (после фикса основных стратегий)  
**Ожидаемый эффект:** Из -$2,787 в +$200-500/месяц  
**Сложность:** 3/5

---

### 2.6 Корреляционный арбитраж

**Решение:** Реализовать как дополнительную стратегию.

**Примеры:**
- "BTC above $100K" YES + "BTC below $100K" YES должны суммироваться в ~$1.00. Если сумма < $0.98, арбитраж.
- SOL 5-min probability стоит 0.50, но Binance показывает чёткий directional candle → mispricing
- BTC dominance рынок vs "BTC up" рынок — если дивергенция, ставь на конвергенцию

**Как построить граф:**
1. Скрейпи все активные рынки через Gamma API
2. Группируй по underlying asset (BTC, ETH, SOL)
3. Для каждой группы проверяй: sum(complementary outcomes) ≈ 1.00
4. Если отклонение > fee (1.8% × 2), есть арбитраж

**Приоритет:** MEDIUM  
**Ожидаемый эффект:** $50-200/месяц, risk-free  
**Сложность:** 3/5

---

## 3. Bankroll Management (Kelly Criterion)

### Формула Kelly для бинарных рынков

```
Kelly % = (P × (1-M) - (1-P) × M) / (1-M)

Где:
P = твоя оценка true probability
M = текущая рыночная цена (market price)
```

**Пример:** Рынок цена YES = $0.60, ты оцениваешь true probability = 75%.
Kelly = (0.75 × 0.40 - 0.25 × 0.60) / 0.40 = **37.5%** bankroll

### Fractional Kelly — используй ЧЕТВЕРТЬ Kelly

- **Full Kelly** — максимизирует рост, но drawdown 50%+. Не для реальной торговли.
- **Half Kelly** — снижает variance на 50%, теряет только 25% скорости роста. Для опытных.
- **Quarter Kelly (0.25x)** — РЕКОМЕНДУЕТСЯ для тебя. Drawdown ~20%, стабильный рост.

При $400 bankroll и Quarter Kelly:
- Kelly показывает 30% → ставка = 0.25 × 30% × $400 = **$30**
- Kelly показывает 10% → ставка = 0.25 × 10% × $400 = **$10**
- Kelly показывает 5% → ставка = 0.25 × 5% × $400 = **$5**

### Размеры ставок по уровню bankroll

| Bankroll | Per-Trade Max | Max открытых позиций | Стратегия |
|----------|--------------|---------------------|-----------|
| $400 | 1-2% ($4-8) базовая / до $30 по Kelly | 2-3 | Только high-edge trades (>5%) |
| $2,000 | 1-2% ($20-40) | 3-5 | Расширение на weather + threshold |
| $10,000 | 2-3% ($200-300) | 5-8 | Добавление MM + копирование |
| $50,000 | 2-3% ($1-1.5K) | 8-15 | Полная диверсификация |

### Stop-Loss правила

- **20% drawdown за день** → уменьши ставки вдвое
- **40% drawdown за неделю** → пауза, review всех стратегий
- **50% drawdown от peak** → полная остановка торговли

### Максимум открытых позиций

При $400: **не более 3 одновременно**. Причина: каждая позиция = 5-10% bankroll, 3 позиции = 15-30% committed. Больше = один плохой день убьёт bankroll.

### Break-even WR по типу fee

| Рыночная цена | Taker (1.8%) break-even WR | Maker (0%) break-even WR |
|---------------|---------------------------|--------------------------|
| $0.30 | ~31.2% | 30% |
| $0.40 | ~41.0% | 40% |
| $0.50 | ~50.9% | 50% |
| $0.60 | ~60.6% | 60% |
| $0.70 | ~70.5% | 70% |

**Вывод:** При 45% WR на CRYPTO_5m (рынки ~$0.50) ты на 6% ниже break-even. Каждый трейд = гарантированный убыток.

---

## 4. Timing и Market Regime Detection

### Определение trending vs choppy

**Для 5m crypto (если решишь вернуться к 15m):**
- **ADX (Average Directional Index):** ADX > 25 = trending, ADX < 20 = choppy
- **Bollinger Band Width:** если ширина < 1% цены = choppy, не торгуй
- **Volume Profile:** объём выше среднего на 20%+ = trending

### Оптимальное время суток

Из твоих данных:
- **Лучшие часы:** 02, 07, 15, 19, 22 UTC (WR 52-60%)
- **Худшие часы:** 00, 03, 04, 13, 16, 18, 21, 23 UTC (WR 29-40%)

**Решение:** Даже если time-of-day filter "не помог WR" — он помог expected value. Торговать с 52% WR лучше чем с 45% WR, даже если трейдов меньше.

Для weather markets: ставь сразу после обновления моделей (00, 06, 12, 18 UTC) — это когда edge максимальный.

### Автоматическое определение деградации стратегии

Реализуй **rolling Sharpe ratio** (50 трейдов):
- Sharpe > 1.0 → нормальная работа
- Sharpe 0.5-1.0 → уменьши ставки до 50%
- Sharpe < 0.5 → пауза на 24 часа
- Sharpe < 0 → полная остановка, review

**Adaptive Kelly:**
```python
def adaptive_kelly(base_kelly, rolling_wr, target_wr=0.55, min_factor=0.1):
    """Уменьшай Kelly когда WR падает"""
    if rolling_wr >= target_wr:
        return base_kelly  # полный Quarter Kelly
    elif rolling_wr >= 0.50:
        factor = (rolling_wr - 0.50) / (target_wr - 0.50)
        return base_kelly * max(factor, min_factor)
    else:
        return 0  # WR ниже 50% = не торгуй
```

**Приоритет:** HIGH  
**Ожидаемый эффект:** Предотвращение слива $1,282+ в будущем  
**Сложность:** 2/5

---

## 5. Техническая оптимизация

### 5.1 LLM: какую модель использовать

**Текущая:** Gemini Flash Lite → 31.5% WR, -$3,532. Это катастрофа.

**Исследование показало:**
- На KalshiBench (300 реальных вопросов) ВСЕ frontier модели показывают systematic overconfidence
- Claude Opus 4.5 — лучше всех по калибровке, но всё ещё с "substantial calibration errors"
- **Ensemble из 3-5 дешёвых моделей ≥ одна дорогая модель** (Science Advances, 2025)

**Решение:**
1. **Ансамбль из 3 моделей:** Gemini Flash + Claude Haiku + GPT-4o-mini
   - Стоимость: ~$0.01-0.03 за запрос (все дешёвые)
   - Усреднение prediction'ов даёт лучшую калибровку чем одна Claude Opus
2. **Temperature scaling post-ensemble:** Усредни предсказания, потом примени scaling
3. **Не используй одну дорогую модель** — ensemble дешевле И точнее

**Приоритет:** MEDIUM (после фикса основных стратегий)  
**Ожидаемый эффект:** С 31.5% WR до потенциальных 52-55%  
**Сложность:** 2/5

### 5.2 Extremizing Alpha

**Текущий alpha = 1.73 — скорее всего слишком агрессивный.**

LLM и так overconfident. Extremizing усиливает confidence. Если LLM говорит 65% и ты extremize до 78%, а реальная probability 55%, ты ставишь на неправильной стороне edge.

**Решение:** Понизить alpha до **1.2-1.4**. Протестируй на исторических данных:
```python
# Platt scaling с extremizing
def extremize(p, alpha=1.3):
    """alpha=1.0 = no change, >1 = push toward 0/1"""
    odds = p / (1 - p)
    new_odds = odds ** alpha
    return new_odds / (1 + new_odds)
```

**Приоритет:** MEDIUM  
**Ожидаемый эффект:** Улучшение калибровки на 5-10%  
**Сложность:** 1/5

### 5.3 Agentic Search

**Решение:** ДА, реализовать. Система AIA Forecaster (agentic news search + forecast reconciliation) **матчит superforecaster-level performance** на ForecastBench.

**Архитектура:**
1. LLM получает вопрос рынка
2. LLM генерирует 3-5 поисковых запросов
3. Fetch результатов (Google News, Twitter, CoinGecko)
4. LLM анализирует результаты + оценивает probability
5. Ensemble averaging

**Приоритет:** MEDIUM  
**Ожидаемый эффект:** Значительное улучшение на event-driven markets  
**Сложность:** 3/5

### 5.4 Размер ансамбля

**Оптимально: 3-5 моделей.** Больше — diminishing returns. Разнообразие моделей важнее их количества. Текущие 2 модели — недостаточно.

---

## 6. Live Trading Readiness

### 6.1 Инфраструктура

**NYC VPS + Frankfurt SOCKS5 proxy** — рабочая схема. Binance WebSocket из NYC = хорошая latency для threshold/weather markets (где миллисекунды не важны). Для 5m/15m crypto markets latency критичнее, но ты их отключаешь.

### 6.2 Maker vs Taker

| Параметр | Maker (0% fee) | Taker (1.8% fee) |
|----------|----------------|-------------------|
| Исполнение | Не гарантировано | Мгновенное |
| Когда использовать | Weather (медленные рынки) | Threshold (нужна точка входа) |
| Min размер ставки | $5+ (иначе spread съест) | Любой |

**Решение:** Используй **maker orders для weather** (рынки медленные, ордера исполнятся) и **taker для threshold** (нужно войти по конкретной цене до того как рынок двинется). Экономия на fee при $400/день trading volume = ~$7/день, $210/месяц.

### 6.3 Slippage на 5m crypto

С dynamic fees на 5m markets, реальный slippage = 2-5% от ставки. При $10 ставке = $0.20-0.50 потеря. Ещё одна причина уйти с этих рынков.

### 6.4 Minimum Viable Bankroll для Live

**$500** — минимум для начала. Но реальный рекомендуемый старт = **$1,000-2,000**. Причина: с $400 и Quarter Kelly твои ставки $5-30, а Polymarket min order = $1. Слишком маленькие ставки = fee доминирует.

**План:**
1. Продолжить paper mode до 3 подряд прибыльных недель
2. Live старт с $500, только THRESHOLD + WEATHER
3. При достижении $2,000 → добавить MM
4. При $5,000 → добавить LLM pipeline

---

## 7. Итоговый план действий (roadmap)

### Неделя 1: Тушим пожары

| # | Действие | Приоритет | Сложность |
|---|---------|-----------|-----------|
| 1 | Отключить CRYPTO_5m | HIGH | 1/5 |
| 2 | Отключить COPY trading | HIGH | 1/5 |
| 3 | Отключить MAIN PIPELINE | HIGH | 1/5 |
| 4 | Реализовать adaptive Kelly + stop-loss | HIGH | 2/5 |

### Неделя 2-3: Масштабирование работающего

| # | Действие | Приоритет | Сложность |
|---|---------|-----------|-----------|
| 5 | Расширить THRESHOLD на ETH/SOL/XRP | HIGH | 2/5 |
| 6 | Добавить term structure арбитраж | HIGH | 3/5 |
| 7 | Перестроить WEATHER (ECMWF + ICON consensus) | HIGH | 3/5 |
| 8 | Переключить weather на maker orders (0% fee) | MEDIUM | 2/5 |

### Неделя 4-6: Расширение

| # | Действие | Приоритет | Сложность |
|---|---------|-----------|-----------|
| 9 | Реализовать correlation arbitrage | MEDIUM | 3/5 |
| 10 | LLM ensemble (3 модели + temperature scaling) | MEDIUM | 2/5 |
| 11 | Понизить extremizing alpha до 1.3 | MEDIUM | 1/5 |
| 12 | Agentic search для event-driven markets | MEDIUM | 3/5 |

### Неделя 7+: Масштабирование

| # | Действие | Приоритет | Сложность |
|---|---------|-----------|-----------|
| 13 | Market making (при bankroll > $2K) | MEDIUM | 4/5 |
| 14 | Copy Trading 2.0 (с фильтрами) | LOW | 3/5 |
| 15 | LASTSEC на 15m markets (увеличенные ставки) | LOW | 2/5 |
| 16 | Переход на live trading | HIGH | 2/5 |

---

## 8. Прогноз результатов

### Консервативный сценарий ($400 start, Quarter Kelly)

| Месяц | Bankroll | Источники прибыли |
|-------|---------|-------------------|
| 1 | $400 → $800 | THRESHOLD ($300) + WEATHER ($100) |
| 2 | $800 → $1,800 | THRESHOLD ($500) + WEATHER ($300) + Arb ($200) |
| 3 | $1,800 → $4,000 | Все стратегии + увеличенные ставки |
| 6 | $4,000 → $15,000 | + Market Making + LLM pipeline |

### Ключевые метрики для отслеживания

- **Rolling 50-trade Sharpe ratio** — должен быть > 1.0
- **Daily PnL** — track отдельно по каждой стратегии
- **Edge decay** — если edge < 3% на weather, рынок стал эффективнее
- **Fee ratio** — fees/gross_profit должен быть < 20%

---

## Источники

- [Weather Bots Making $24K on Polymarket](https://blog.devgenius.io/found-the-weather-trading-bots-quietly-making-24-000-on-polymarket-and-built-one-myself-for-free-120bd34d6f09)
- [Arbitrage Bots Dominate Polymarket (Yahoo Finance)](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- [4 Polymarket Strategies Bots Profit From in 2026](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Polymarket Liquidity Rewards Docs](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Polymarket CLOB Docs](https://docs.polymarket.com/developers/CLOB/introduction)
- [Polymarket Fees](https://docs.polymarket.com/trading/fees)
- [Kelly Criterion for Prediction Markets (arXiv)](https://arxiv.org/html/2412.14144v1)
- [KalshiBench: LLM Calibration](https://arxiv.org/html/2512.16030)
- [AIA Forecaster (arXiv)](https://arxiv.org/html/2511.07678v1)
- [LLM Ensemble Forecasting (Science Advances)](https://www.science.org/doi/10.1126/sciadv.adp1528)
- [poly-maker GitHub](https://github.com/warproxxx/poly-maker)
- [Polymarket Weather Bot GitHub](https://github.com/suislanchez/polymarket-kalshi-weather-bot)
- [Polymarket Dynamic Fees (Finance Magnates)](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- [5-Minute Markets Trading Guide (Polyblock)](https://polyblock.trade/info/guides/polymarket-5-minute-markets)
- [GFS vs ECMWF Accuracy (Celsi)](https://celsi.markets/blog/gfs-vs-ecmwf-forecast-accuracy)

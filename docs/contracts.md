# TALOS Data Contracts & Invariants

Цей документ визначає жорсткі правила для обміну даними всередині системи TALOS.
Порушення цих правил ламає пайплайн і заборонено на рівні CI/CD.

## 1. Single Source of Truth
Файл **`src/core/types.py`** — єдине джерело істини.
* **Заборонено:** Передавати "сирі" словники (`dict`) між модулями SDR, Logic та API.
* **Вимагається:** Використовувати Pydantic-моделі для будь-якої передачі даних.
* **Виняток:** Сирі dict/JSON допустимі лише на межі системи (API/DB/WS ingress), але мають бути одразу перетворені на DTO через `Model.model_validate()`.

---

## 2. Технічні Інваріанти (Hard Rules)

### 2.1 Strict Schema
Всі моделі успадковуються від `TalosBaseModel`.
* **`extra="forbid"`**: Передача полів, не описаних у схемі — це помилка (Critical Error). Ми не засмічуємо логи та бази даних "сміттям".
* **Frozen Configs**: Конфігураційні об'єкти (`ProcessingConfig`, `SdrConfig`) — імутабельні (`frozen=True`).

### 2.2 Time & Dates
* **UTC Only**: Всі `timestamp` зберігаються та передаються виключно в UTC.
* **Naive Datetime**: Заборонені (викликають `ValidationError`).
* **Aware Datetime**: Дозволені, але автоматично нормалізуються в UTC під час валідації.
* **Helper**: Використовуйте `ensure_utc()` або `default_factory=utc_now`.

### 2.3 Naming & Units
Назва поля повинна містити одиницю виміру (SI units).
* `frequency` ❌ → `center_freq_hz` ✅
* `gain` ❌ → `gain_db` ✅
* `duration` ❌ → `duration_s` (або `_ms`) ✅

---

## 3. Протокол змін (Change Protocol)

Якщо ви хочете додати нове поле у модель, ви маєте відповісти на 3 питання у PR:

1.  **Producer:** Хто і де гарантовано заповнить це поле?
2.  **Consumer:** Хто читатиме це поле? (Database, Frontend, Alerting).
3.  **Rationale:** Навіщо це поле зараз? (Поля "на майбутнє" заборонені).

**Optional поля:** Допускаються лише за наявності чіткої причини (недоступність даних для Producer) та визначеної поведінки Consumer при `null`.

**Приклад Bad Practices:**
* `data: dict` (Structureless blob)
* `timestamp: datetime` (Unknown timezone)
* `temp: 45` (Celsius? Fahrenheit?)
# TALOS Data Contracts & Invariants

Цей документ визначає жорсткі правила для обміну даними всередині системи TALOS.
Порушення цих правил ламає пайплайн і заборонено на рівні CI/CD.

Документ описує **інваріанти системи**, які повинні виконуватися у всіх модулях:

SDR → DSP → Logic → API → UI

Це **технічний контракт між модулями системи**.

Архітектурний опис системи знаходиться у:
`docs/talos_architecture.md`

---

# 1. Single Source of Truth

Файл **`src/core/types.py`** — єдине джерело істини для моделей даних.

## Заборонено

Передавати "сирі" словники (`dict`) між модулями:

- SDR
- DSP
- Logic
- API
- Storage
- AI worker

## Вимагається

Будь-яка передача даних всередині системи повинна використовувати **Pydantic DTO**.

```
TalosBaseModel
```

## Виняток

Сирі `dict` / `JSON` дозволені лише **на межі системи**:

- HTTP API
- WebSocket ingress
- Database IO
- CLI / scripts

Після входу у систему вони **негайно перетворюються у DTO**:

```
Model.model_validate(payload)
```

---

# 2. Pipeline Data Model

TALOS використовує багаторівневий pipeline обробки сигналів.

```
RF Frontend
   ↓
IQ Stream
   ↓
Spectrum Estimation (FFT)
   ↓
Signal Detection (CFAR)
   ↓
Signal Tracking
   ↓
Signal Classification (AI)
   ↓
Decision Logic
   ↓
Alert
```

Основні DTO pipeline:

| Layer | DTO |
|-----|-----|
| Spectrum telemetry | `WaterfallFrame` |
| Detection | `CfarEvent` |
| Decision | `Alert` |

---

# 3. Frame Synchronization Model

Для синхронізації телеметрії використовується **frame_seq**.

```
frame_seq = монотонний номер FFT кадру
```

Його генерує **DSP engine (`SdrMonitor`)**.

## Інваріанти

1. `frame_seq` **монотонно зростає**
2. `frame_seq` **ніколи не генерується у кількох процесах**
3. `frame_seq` використовується для трасування подій

```
WaterfallFrame
CfarEvent
Alert
```

Pipeline синхронізації:

```
FFT frame
   ↓
WaterfallFrame(frame_seq)
   ↓
CFAR detection
   ↓
CfarEvent(source_frame_seq)
   ↓
Alert(source_frame_seq)
```

Це дозволяє UI точно визначити:

```
цей сигнал → цей рядок водоспаду
```

---

# 4. Telemetry Streams

TALOS використовує три типи потоків даних.

## 4.1 Spectrum Telemetry

DTO:

```
WaterfallFrame
```

Характеристики:

- дуже висока частота
- streaming
- lossy

Політика доставки:

```
latest frame wins
```

Старі кадри можуть бути **відкинуті без помилки**.

Це необхідно для стабільної роботи DSP.

---

## 4.2 Detection Events

DTO:

```
CfarEvent
```

Характеристики:

- середня частота
- важливі для DSP аналізу
- повинні містити `source_frame_seq`

---

## 4.3 Decision Events

DTO:

```
Alert
```

Характеристики:

- низька частота
- критично важливі
- зберігаються у базі

---

# 5. Технічні Інваріанти (Hard Rules)

## 5.1 Strict Schema

Всі моделі повинні успадковуватися від:

```
TalosBaseModel
```

Конфігурація:

```
extra="forbid"
```

Це означає:

- передача невідомих полів = **Critical Error**
- схема не може "розповзатися"

---

## 5.2 Frozen Configurations

Конфігурації системи **імутабельні**.

Використовується:

```
TalosFrozenModel
```

Приклад:

```
ProcessingConfig
SdrConfig
TalosConfig
```

Runtime зміна конфігурацій **заборонена**.

---

## 5.3 Time & Dates

Всі timestamps:

```
UTC only
```

### Заборонено

```
naive datetime
```

### Дозволено

```
timezone aware datetime
```

Але вони автоматично **нормалізуються до UTC**.

Helpers:

```
utc_now()
ensure_utc()
```

---

## 5.4 Naming & Units

Назва поля **обов'язково містить одиницю виміру**.

Приклади:

```
frequency      ❌
center_freq_hz ✅
```

```
gain       ❌
gain_db    ✅
```

```
duration   ❌
duration_s ✅
```

Це правило робить API самодокументованим.

---

# 6. DTO Evolution Protocol

Будь-яка зміна у `types.py` вимагає **Architecture Review**.

У Pull Request потрібно відповісти на 3 питання.

## Producer

Хто створює це поле?

```
DSP
Logic
API
AI Worker
```

## Consumer

Хто читає це поле?

```
UI
Alerting
Storage
ML pipeline
```

## Rationale

Навіщо це поле потрібно **саме зараз**?

Поля "на майбутнє" **заборонені**.

---

# 7. Optional Fields Policy

`Optional` поля дозволені лише якщо:

- producer **іноді не має цих даних**
- consumer **має визначену поведінку для null**

Приклади:

```
duration_s
duty_cycle
noise_floor_db
```

---

# 8. Bad Practices (Forbidden)

Наступні патерни заборонені:

```
data: dict
```

(структурно невизначений blob)

```
timestamp: datetime
```

(невідомий timezone)

```
temp: 45
```

(невідомі одиниці)

---

# 9. CI Validation

CI повинен перевіряти:

- Pydantic schema validation
- відсутність `dict` у pipeline
- коректність UTC timestamps
- відповідність DTO contracts

Будь-яке порушення інваріантів = **CI failure**.

---

# Summary

Основні принципи TALOS Data Contracts:

1. **Typed data only**
2. **UTC timestamps**
3. **Explicit units**
4. **frame_seq synchronization**
5. **No schema drift**

Ці правила гарантують, що система залишатиметься:

- детермінованою
- дебажною
- масштабованою
- придатною до реального RF моніторингу
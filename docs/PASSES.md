# Проходы резки: подробный разбор

Документ для запуска по одной задаче: один проход = одна область = один коммит с числами. Решения по областям — в `docs/DECISIONS.md`. Числа базлайна — снимок `ae3f9f9`.

Инструменты в репозитории:

```bash
# Инвентаризация дерева: файлы и строки по областям (числа «до» и «после»)
python3 scripts/znth/inventory_tree.py --root . --top 30

# Строгий импорт: каждый модуль обязан разрешиться внутри форка, а не в чужой копии
python3 scripts/znth/strict_import_check.py --root /root/znth-agent \
    --drop /usr/local/lib/hermes-agent \
    hermes_cli.main model_tools tools.browser_tool agent.web_search_registry
```

Строгая проверка обязательна после **каждого** прохода: ленивые импорты (`try/except ImportError` внутри функции) не мешают дереву собраться и падают только на вызове. Проверка без `--drop` бесполезна на этой машине: рядом лежит вторая копия Hermes (`/usr/local/lib/hermes-agent`), и импорт разрешится в неё.

---

## Проход 1. Поиск: режем чужое облако

**Область:** `plugins/web/`.

**Удаляем (нужен чужой ключ или вендорный шлюз):** `brave_free`, `exa`, `firecrawl`, `keenable`, `parallel`, `perplexity`, `tavily`, `xai`.

**Оставляем:** `ddgs` (без ключа), `searxng` (свой сервер), `keyless_mcp.py`, `_common.py`, `__init__.py`. `openai_native` — уточнить: своего ключа не требует, ходит через уже настроенного провайдера.

**Корень, с которого начинать:** реестр `agent/web_search_registry.py` — остальные списки лишь повторяют его состав. Дальше: `hermes_cli/config_defaults.py`, `hermes_cli/dashboard_register.py`, `hermes_cli/models_validate.py`, `hermes_cli/nous_subscription.py`, шаг мастер-настройки.

**Проверка:** построить действующий набор провайдеров разрешением (не чтением списка); прогнать поиск на `ddgs` и на своём `searxng`.

## Проход 2. Браузинг: режем облачные бэкенды, оставляем локальные

**Область:** `plugins/browser/`.

**Удаляем:** `browser_use` (managed), `browserbase`, `firecrawl`.

**Оставляем:** локальные движки — Lightpanda, Camofox, CDP, локальный `browser-use` CLI, реальный профиль браузера.

**Шов, который нельзя вырезать целиком:** `tools/browser_tool_cloud.py` держит не только облачный резолвер, но и общие помощники конфигурации (headed-режим, выбор движка, локальность, политика приватных URL). Заглушаем **только** `_get_cloud_provider()` → `None`, файл оставляем и помечаем маркером `impulse:` с триггером.

**Там же:** `agent/browser_registry.py::_LEGACY_PREFERENCE` — автоподбор облачных провайдеров; после резки кортеж пустой.

**Проверка:** `browser_navigate` на локальном движке проходит; в конфиге `browser.cloud_provider` пусто или `local`; резолвер при вызове печатает «не настроено», а не «верю, что он вызывается».

## Проход 3. Платформы: из 21 канала остаётся Telegram

**Удаляем каталоги** `plugins/platforms/`: `a2a`, `buzz`, `dingtalk`, `discord`, `email`, `feishu`, `google_chat`, `irc`, `line`, `matrix`, `mattermost`, `ntfy`, `photon`, `raft`, `simplex`, `slack`, `sms`, `teams`, `wecom`, `whatsapp`.

**Оставляем:** `telegram` целиком — группы, топики, реакции, голосовые (решение владельца).

**Корень:** реестр `gateway/platform_registry.py`; затем шаг «платформы» в `hermes_cli/setup.py`, значения по умолчанию конфига, записи в каталоге плагинов, доки.

**Разделять две разные вещи:**

- `plugins/platforms/*` — каналы связи (то, что режем).
- `gateway/platforms/*.py` — общий транспорт и серверные швы (`base.py`, `webhook.py`, `api_server*.py`, `signal.py`, `whatsapp_cloud.py`, `yuanbao*.py`, `bluebubbles.py`, `msgraph_webhook.py`, `tcp_site.py`, `media_cache.py`, `shared_ingress.py` и т.д.). Это **не** «платформы-плагины». Их судьба — отдельное решение; удалять их вместе с каналами значит унести транспорт Telegram тоже.

**Проверка:** гейтвей поднимается, сообщение в Telegram принимается и отправляется, в списке доступных платформ нет ни одного удалённого имени.

## Проход 4. Провайдеры моделей: четыре профиля

**Оставляем:** `opencode-zen` (в этом каталоге объявлены **два** профиля: `opencode-zen` и `opencode-go`), `gemini`, `custom` (openai-custom).

**Удаляем** остальные ~37 каталогов: `actual`, `ai-gateway`, `alibaba`, `alibaba-coding-plan`, `anthropic`, `arcee`, `azure-foundry`, `bedrock`, `commandcode`, `copilot`, `copilot-acp`, `deepinfra`, `deepseek`, `fireworks`, `gmi`, `huggingface`, `kilocode`, `kimi-coding`, `lmstudio`, `meta-ai`, `minimax`, `nebius-token-factory`, `nous`, `novita`, `nvidia`, `ollama-cloud`, `openai-codex`, `openrouter`, `qwen-oauth`, `router`, `solstice`, `stepfun`, `tencent-tokenhub`, `upstage`, `vertex`, `xai`, `xiaomi`, `zai`.

**Единица удаления — объявление, а не каталог.** Каталог может держать несколько профилей (как `opencode-zen`): считать `name=` в вызовах `register_provider(ProviderProfile(...))`, иначе уносим соседа.

**Новая возможность (в апстриме её нет):** нативный Gemini-вайр должен работать с любым `base_url` при авторизации по ключу. Сейчас вайр выбирается по базовому URL: `is_native_gemini_base_url()` (`agent/gemini_native_adapter.py`) отвечает True только для хостов `generativelanguage.googleapis.com` и Vertex Express, всё остальное уходит в chat-completions. Правка на несколько строк, годная в апстрим.

**Проверка:** разрешённый список профилей содержит ровно четыре имени; запрос к Gemini через произвольный `base_url` идёт нативным вайром (проверять разбором выбранного вайра, а не намерением).

## Проход 5. Наборы инструментов: главный рычаг «легче»

**Удаляем из `toolsets.py`** (построчно, по границам скобок — запись это вызов функции, а не литерал): `homeassistant`, `spotify`, `yuanbao`, `feishu_doc`, `feishu_drive`, `discord`, `discord_admin`, `image_gen`, `video_gen`, `tts`, `x_search`, `bot_room`, `setup`.

**Две поверхности, обе обязательны:**

1. файлы инструментов (`tools/<имя>_tool*.py`) и плагины, их регистрирующие (`plugins/image_gen`, `plugins/video_gen`; привязку `plugins/google_meet` и `plugins/teams_pipeline` проверить на месте);
2. имена в наборах — `toolsets.py`, `_HERMES_CORE_TOOLS`, и чеклист мастера `CONFIGURABLE_TOOLSETS` (`hermes_cli/tools_config.py`, 27 записей). Чеклист и есть та «вереница ненужной хрени», о которой говорил владелец.

**Записи каталога:** `plugin-catalog/homeassistant.yaml`, `plugin-catalog/spotify.yaml`, `plugin-catalog/spotify-desktop.yaml` — снимать вместе с наборами, иначе каталог продолжит предлагать установку вырезанного.

**Проверка:** построить **разрешённый** набор инструментов (часть наборов регистрируют сами инструменты при импорте, статический список не сходится с тем, что видит модель); вызвать мастер настройки и убедиться, что удалённых пунктов в чеклисте нет.

## Проход 6. Локали и тексты апстрима

**`locales/`:** оставить `en.yaml`, `ru.yaml`, `en.tui.yaml`, `ru.tui.yaml` (плюс `_keys.tui.json`, `keys.desktop.json`) — удалить 13 языков: `af`, `ar`, `de`, `es`, `fr`, `ga`, `hu`, `it`, `ja`, `ko`, `pt`, `tr`, `uk`, `zh`, `zh-hant` в обоих вариантах.

**Тексты:** удалить `README.es.md`, `README.zh-CN.md`, `README.ur-pk.md`, `SECURITY.es.md`, `CONTRIBUTING.es.md` — свои тексты появятся на фазе ребрендинга (P4).

**Проверка:** интерфейс работает на `ru` и `en`; выбор удалённого языка недоступен.

## Проход 7. `optional-skills/` целиком

860 файлов, 155 `SKILL.md`, 15 МБ, 22 категории. Каталог не активен, но сидируется механизмом скиллов — после удаления проверить, что сидирование встроенных скиллов осталось живым.

## Проход 8. Скиллы: до минимума, потом своя библиотека

Встроенных 58 в 12 категориях. Порядок важен и он из правил владельца:

1. сперва **влить** в свою версию всё полезное из встроенного — ничего не удаляя у себя;
2. затем удалить встроенный **отдельным проходом**, чтобы было видно, что именно влилось, а что признано дублем;
3. свою библиотеку (impulse) положить в каталог встроенных скиллов форка — тогда она сидируется на любой установке, а не раскладывается руками.

**Проверка:** сверка имён со встроенным набором (пустое пересечение означает «рядом», а не «поверх»); каждый `SKILL.md` проходит парсер фронтматтера; категория выводится из первого уровня каталога.

---

## Сводка по порядку

| № | Проход | Файлов под нож | Риск |
|---|---|---|---|
| 1 | Поиск (облачные провайдеры) | 8 каталогов | низкий |
| 2 | Браузер (облачные бэкенды) | 3 каталога + заглушка резолвера | средний (шов с общими помощниками) |
| 3 | Платформы (20 каналов) | 20 каталогов | средний (не задеть `gateway/platforms/`) |
| 4 | Провайдеры моделей | ~37 каталогов | средний (несколько профилей в каталоге) |
| 5 | Наборы инструментов | `toolsets.py` + чеклист + плагины + записи каталога | высокий (две поверхности + мастер) |
| 6 | Локали и тексты | 13 языков + 5 файлов README/SECURITY/CONTRIBUTING | низкий |
| 7 | `optional-skills/` | 860 файлов | низкий |
| 8 | Встроенные скиллы → минимум + impulse | 58 пакетов | средний (правило слияния) |

Порядок от низкого риска к высокому: сначала то, что не имеет швов с ядром. Проход 5 (инструменты) идёт последним среди крупных — он затрагивает и наборы, и мастер, и записи каталога.

---

## Журнал проходов

### Проход 1 — поиск: облачные провайдеры. СДЕЛАНО (2026-10-13)

**Что срезано (С ПЕРЕСМОТРОМ — см. «Пересмотр прохода 1» в конце):** первый заход резал все восемь облачных вендоров плюс keyless-кольцо. После уточнения правила владельцем («если что-то не требует ключа и просто работает — оставляем») откат и новый рез: остались `exa`, `parallel`, `keenable`, `tavily`, `firecrawl` (keyless-тир + self-hosted) и keyless-кольцо; под нож пошли только те, кому нужен чужой ключ или аккаунт — `brave_free`, `perplexity`, `xai`, `openai-native` (27 файлов, −231 строка). Историческую запись ниже читать вместе с пересмотром.

**Что было швом и почему простое удаление каталогов не годилось:**

- `tools/web_tools.py` импортировал функции firecrawl-плагина **на верхнем уровне** — удаление каталога уронило бы весь поиск. Импорт снят, использования вычищены.
- Дефолтом бэкенда при пустой конфигурации стоял `firecrawl` (чужой провайдер). Теперь пустая строка: диспетчер выдаёт чистое «no web search provider configured» вместо имени, которого никто не обслуживает.
- `hermes_cli/web_routers/tools.py` (дашборд) тянул из того же плагина `is_managed_route`.
- Managed-маршрут Nous (`nous` → perplexity/firecrawl) и one-shot keyless-rescue заглушены, а не вырваны: `_managed_web_search()` → `False`, `tools/web_tools_rescue.py` сведён к швам, которые зовёт диспетчер.

**Проверено (вывод команд):**

- Строгий импорт: 10 модулей (`hermes_cli.main`, `model_tools`, `tools.web_tools`, `tools.web_tools_rescue`, `tools.web_tools_extract`, `agent.web_search_registry`, `plugins.web._common`, `plugins.web.ddgs.provider`, `plugins.web.searxng.provider`, `hermes_cli.web_routers.tools`) — все разрешились внутри `/root/znth-agent`.
- Действующий набор провайдеров (разрешение, не чтение списка): `['ddgs', 'openai-native', 'searxng']`. Снятых имён нет.
- Порядок автоподбора: `('searxng', 'ddgs')`; keyless-тир `()` и `False`.
- Выбор снятого провайдера даёт честную ошибку, а не тихую подмену: «web is configured to use 'exa' … no registered web search provider has that name. Run 'hermes tools' to change it.»
- Сборка тестов: `pytest --collect-only` по 8 файлам, связанным с поиском — 106 тестов собираются, мёртвых по импорту нет.

**Не проверено (говорить честно):** живой поиск не гонялся — на этой машине не установлен `ddgs` и не задан `SEARXNG_URL`, поэтому `check_web_api_key()` возвращает `False` и инструменты поиска скрыты от модели. Первый же живой прогон — на машине с селфхостедным SearXNG или с установленным `ddgs`.

### Проход 1b — по решениям владельца. СДЕЛАНО (2026-10-13)

**Firecrawl возвращён, но только как свой инстанс.** Внутри провайдера было три маршрута и селфхостед; оставлен один:

- `_get_direct_firecrawl_config()` требует только `FIRECRAWL_API_URL` — облачный ключ больше не считается маршрутом;
- вырезаны анонимное keyless-кольцо (`_use_keyless_ring` → заглушка), вендорский шлюз (`_get_firecrawl_gateway_url` удалён), keyless-клиент (`_KeylessFirecrawlClient` → заглушка с маркером), ветки кольца в `search()`/`extract()`;
- `is_managed_route()` и `_is_tool_gateway_ready()` оставлены заглушками с маркерами `impulse:` — так тесты, которые их патчат, не падают на импорте;
- кэш клиента переехал из `tools.web_tools` (слоты удалены вместе с резкой) в сам модуль плагина;
- `get_setup_schema()` и `plugin.yaml` описывают свой инстанс.

**`openai-native` срезан** целиком (3 файла): требовал OAuth-аккаунт `openai-codex`. Код в `agent/transports/codex.py`, распознающий это имя, оставлен — безвреден, уйдёт вместе с транспортом в проходе по провайдерам моделей.

**Мастер избавлен от мёртвых строк:** в разделе `web` больше нет рукописных записей — ни managed-строки «Nous Subscription», ни дубля «Firecrawl Self-Hosted» (теперь строку даёт сам плагин). Остались три: `ddgs`, `searxng`, `firecrawl (self-hosted)`.

**Проверено:** строгий импорт 10 модулей (все внутри `/root/znth-agent`, включая правленый `plugins/web/firecrawl/provider.py`, `hermes_cli/tools_config.py` и роутер дашборда); действующий набор провайдеров разрешением — `['ddgs', 'searxng', 'firecrawl']`; `is_available()` у firecrawl без своего URL возвращает False (проверяется тем, что `check_web_api_key()` даёт False и автоподбор не выбирает firecrawl).

**Хвосты (сверено после решений владельца):**

1. **Firecrawl — РЕШЕНО (проход 1b).** Каталог возвращён и сведён к своему инстансу: облако по чужому ключу, анонимное кольцо и вендорский шлюз вырезаны, `_is_tool_gateway_ready`/`is_managed_route` — заглушки с маркерами, строка мастера теперь одна (её даёт плагин).
2. **`openai-native` — РЕШЕНО (проход 1b).** Срезан целиком: требовал OAuth-аккаунт `openai-codex`. Код в `agent/transports/codex.py`, распознающий это имя, оставлен — он безвреден и уйдёт вместе с транспортом в проходе по провайдерам моделей.
3. **`web_extract` без extract-бэкенда — ЧАСТИЧНО закрыт:** self-hosted Firecrawl умеет extract, так что с поднятым своим инстансом инструмент работает. Свой локальный конвертер страниц (HTTP-фетч + markdown, как в openclaude) — отдельная задача, в плане.
4. **8 тестовых файлов** (`test_web_managed_routing`, `test_web_tools_perplexity`, `test_web_picker_active_rows`, `test_web_providers`, `test_web_backend_capability_isolation`, `test_web_tools_config`, `test_web_providers_searxng`, `test_web_extract_robustness`) собираются, но часть их кейсов покрывает снятое. Тесты скоупа владелец решил держать целиком, поэтому файлы не тронуты — их надо подрезать точечно.
5. **Managed-шлюз (вендорский маршрут)** жив в `tools/managed_tool_gateway.py`, `hermes_cli/nous_subscription.py` и используется другими подсистемами (image_gen, tts, transcription, modal). В резке поиска он только заглушен; снимать ли механизм целиком — отдельное решение.

### Проход 2 — облачные браузеры. СДЕЛАНО (2026-10-13)

**Что срезано:** `plugins/browser/` удалён целиком — провайдеры `browser_use` (managed Browser Use), `browserbase`, `firecrawl` и их общая база `_common.py` (жизненный цикл облачных сессий, 138 строк). Всего 15 файлов, −574 строки.

**Швы:**

- `tools/browser_tool_cloud.py` импортировал оба провайдера **на верхнем уровне** — удаление каталогов уронило бы все браузерные инструменты. Импорты сняты.
- `_autodetect_cloud_provider()` → заглушка `None` с маркером: walk по облачным вендорам убран, поэтому никогда не настроенная установка садится на локальный браузер.
- Из `_resolve_cloud_provider_uncached()` убрана привязка managed-выбора `nous` → `browser-use`.
- `agent/browser_registry.py::_LEGACY_PREFERENCE` пуст. Резолвер по имени **не тронут** — пользовательский плагин, зарегистрировавший браузерного провайдера, по-прежнему достаётся явным `browser.cloud_provider: <имя>`.
- Поверхности выбора: `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `BROWSER_USE_API_KEY` убраны из конфига; из мастера убрана строка «Nous Subscription (Browser Use cloud)». В разделе браузера остались только локальные строки: Local Browser, Lightpanda, Camofox, Browser Use (локальный CLI).
- В перечислении метрик значения `browser-use`/`browserbase`/`firecrawl` оставлены (это контракт меток для дашбордов), поправлен только устаревший комментарий.

**Проверено (вывод команд):**

- Строгий импорт: 9 модулей (`model_tools`, `tools.browser_tool`, `tools.browser_tool_cloud`, `tools.browser_use_cli`, `tools.browser_lightpanda`, `agent.browser_registry`, `agent.browser_provider`, `hermes_cli.tools_config`, `hermes_cli.config_defaults`) — все внутри `/root/znth-agent`.
- Реестр браузерных облачных провайдеров: `none`; `_LEGACY_PREFERENCE` = `()`.
- `_autodetect_cloud_provider()` → `None`; `_get_cloud_provider()` → `None`; `_is_local_mode()` → `True`; `browser_backend_name()` → `'local'`.
- Локальный движок `browser-use` CLI на месте (`tools/browser_use_cli.py`, `_BACKEND_KEY = browser-use`).

**Не проверено (честно):** живой прогон навигации не выполнялся — проверено разрешение режима (локальный) и импорты, но не фактический запуск браузера.

### Пересмотр прохода 1 — уточнённое правило (2026-10-13)

Уточнение владельца: «давай exa оставим, и если что-то не требует ключа и просто работает — оставляем».

**Что сделано:** оба захода прохода 1 (`a6e3bef8`, `942987bb`) откачены в веб-области — файлы поиска возвращены из базлайна `817d8851` (провайдеры, keyless-кольцо, keyless-помощники в `_common.py`, реестр, вызовы, тесты). Затем вырезано заново только то, что требует ключа или аккаунта. Откат и новый рез — сознательно, чтобы не латать полурез: «вернуть пачку целиком, а потом резать заново только согласованное».

**Резано (27 файлов, −231 строка):** `plugins/web/{brave_free, perplexity, xai, openai_native}` и три тестовых файла (`test_web_providers_brave_free.py`, `test_web_providers_xai.py`, `test_web_tools_perplexity.py`). Все четыре требуют чужого ключа или OAuth-аккаунта даже на бесплатном пути.

**Остались и работают без единого ключа:** `exa`, `parallel`, `keenable` (анонимные бесплатные тиры), `tavily` (опционально keyless), `firecrawl` (keyless-облако + свой инстанс), `ddgs` (без ключа), `searxng` (селфхостед). Keyless-кольцо и one-shot rescue — как в апстриме.

**Поверхности подчищены точечно:** `_LEGACY_PREFERENCE` без perplexity и brave-free; `_BUILTIN_AVAILABILITY` без perplexity/brave-free/xai; функция `_xai_available` удалена (её веб-провайдер срезан, а `tools/xai_http.py` для TTS и медиа не тронут); `_WEB_CHECK_SKIP` пуст; подсказка extract-бэкендов называет только живых; ключи PERPLEXITY и BRAVE убраны из конфига, а EXA/PARALLEL/TAVILY/KEENABLE возвращены (они опциональны); у `nous`-выбора search больше не привязан к perplexity — managed-маршрут теперь всегда gateway Firecrawl.

**Проверено (вывод команд):**

- Строгий импорт: 14 модулей — все разрешились внутри `/root/znth-agent`.
- Действующий набор провайдеров: `['ddgs', 'exa', 'firecrawl', 'keenable', 'parallel', 'searxng', 'tavily']`; снятых имён нет.
- Keyless-тир включён, порядок кольца `('parallel', 'firecrawl', 'keenable', 'exa')`.
- `check_web_api_key()` → `True` (до пересмотра было `False`).
- **Живой поиск прошёл:** `web_search_tool("test")` вернул `success: true` с настоящими результатами через keyless-тир Exa — ни одного ключа поиска на машине нет.

**Не проверено:** живой `web_extract` (страницу не тянула) — но extract-бэкендом выбран `exa`, у которого `supports_extract()` истинно, так что маршрут тот же.


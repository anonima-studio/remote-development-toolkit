# Быстрый старт

> Перевод. Источники: [docs/quickstart.md](../quickstart.md) и раздел
> «Ограничения» по [docs/limitations.md](../limitations.md). При расхождении
> верен английский текст.

От нового сервера Ubuntu до панели Coder на вашем Mac одной командой, затем
первый workspace. Здесь используется минимальная топология: ничего не
публикуется в интернет, панель открывается через SSH-проброс, workspace
выходят в интернет через маршрут самого сервера.

## Требования

### Mac (клиент оператора)

Клиент — macOS. Скрипты работают с системным Bash 3.2.

| Инструмент | Для чего | Установка |
| --- | --- | --- |
| OpenSSH, `ssh-keygen`, `curl`, `nc` | для всего | входят в macOS |
| Git и Python 3 | для всего | `xcode-select --install` |
| [Homebrew](https://brew.sh) | установка инструментов ниже | см. сайт |
| Coder CLI | workspace, Codex Desktop, Local Agent, секреты | `brew install coder` |
| Node.js 24 и pnpm (через Corepack) | проверки репозитория, Local Agent | `brew install node@24`, затем `corepack enable` |
| Mutagen ровно 0.18.1 | только режим `local-agent` | `brew install mutagen-io/mutagen/mutagen` |
| Codex Desktop или Codex CLI | только необязательный адаптер Codex | от OpenAI |

Версия Coder CLI должна быть близка к серверной (Coder `v2.36.0`), иначе SSH
сообщит о несовместимости клиента и сервера.

### Сервер

- Новый сервер **Ubuntu 24.04 или 26.04**, x86_64. Другие версии bootstrap
  отклоняет. Пакет Sysbox для вложенного Docker есть только для amd64.
- Административный вход по SSH-ключу: `root` или пользователь с `sudo`. После
  bootstrap вход под root и по паролю отключается.
- Исходящий HTTPS к репозиториям Ubuntu и Docker, к реестрам контейнеров
  (`ghcr.io`, Docker Hub) и к реестру Terraform.
- Рекомендуемый размер: **от 4 vCPU, от 8 GiB RAM, от 80 GB диска.** Это
  рекомендации, а не проверяемый минимум. Coder и PostgreSQL вместе ограничены
  3 CPU и 3 GiB; каждый workspace добавляет свой жёсткий лимит (`standard` —
  2 CPU и 4 GiB).

Больше ничего не нужно. Расширенная топология (публичное HTTPS-имя и выход
через VPN) необязательна и требует сторонних учётных данных; см.
[advanced-topology.md](../advanced-topology.md).

## Запуск

```bash
git clone https://github.com/anonima-studio/remote-development-toolkit.git remote-dev
cd remote-dev
bin/remote-dev quickstart example-profile --ssh root@203.0.113.10
```

Замените `example-profile` коротким именем сервера (строчные буквы, цифры и
`-`), а `203.0.113.10` — адресом сервера. Параметры:

| Параметр | По умолчанию | Смысл |
| --- | --- | --- |
| `--ssh <user>@<server>` | обязателен | Первый административный вход (`root` или пользователь с sudo) |
| `--admin-key <путь>` | ваш SSH-агент и ключи по умолчанию | Приватный ключ для этого входа, например ключ, установленный провайдером |
| `--key <путь>` | `~/.ssh/remote-dev` | Ключ оператора для пользователя `dev`; создаётся, если его нет |
| `--local-port <порт>` | `3080` | Порт на Mac для проброса панели |
| `--host-port <порт>` | `3080` | Порт Coder на loopback сервера |

## Что делает команда

Команда печатает заголовок `==>` для каждого шага.

1. **Ключ оператора.** Использует `~/.ssh/remote-dev` или создаёт там ключ
   ed25519 без пароля. Пароль можно добавить позже:
   `ssh-keygen -p -f ~/.ssh/remote-dev`.
2. **Подготовка хоста.** Если `dev@<сервер>` уже работает с этим ключом, шаг
   пропускается. Иначе команда открывает административный вход, держит
   соединение до 15 минут и запускает
   [`bootstrap/ubuntu.sh`](../../bootstrap/ubuntu.sh) через `sudo`. Скрипт
   обновляет пакеты, включает автоматические обновления безопасности, создаёт
   пользователя `dev` с вашим ключом и sudo без пароля, ставит Docker и restic,
   ужесточает SSH (без root, без паролей, `AllowUsers dev`) и включает UFW с
   единственным открытым TCP/22. Затем команда проверяет новый вход `dev` с
   sudo без пароля и Docker и только после этого закрывает административное
   соединение.
3. **SSH-алиас и профиль.** Пишет `~/.ssh/remote-dev-server-<profile>.conf`
   (`Host <profile>` → `dev@<сервер>` с ключом оператора и `IdentitiesOnly`) и
   добавляет строку `Include` в начало `~/.ssh/config`. Создаёт
   `deploy/host-profiles/<profile>.env` с минимальной топологией, если файла
   ещё нет.
4. **Установка Coder и шаблона workspace.** Запускает `install`: загружает
   развёртывание, собирает образ workspace, запускает Coder и PostgreSQL на
   loopback сервера, создаёт учётную запись владельца и сессию оператора,
   импортирует шаблон **Remote Dev Docker** и задаёт автоостановку через восемь
   часов. Первый запуск собирает образ workspace; вместе с обновлением системы
   это занимает 15–30 минут.
5. **Doctor.** Запускает `doctor`, который печатает по строке `OK`, `FAIL` или
   `SKIP` на каждую проверку.
6. **Следующие шаги.** Печатает команды из следующего раздела.

Первое SSH-подключение использует `StrictHostKeyChecking=accept-new`: ключ
сервера принимается при первом подключении и проверяется в дальнейшем.

## Ожидаемый вывод

Сокращённо; адреса и пути у вас другие:

```text
==> Operator key
Created  ~/.ssh/remote-dev (no passphrase; add one with ssh-keygen -p -f ~/.ssh/remote-dev)

==> Host bootstrap
...
Verified dev@203.0.113.10 with passwordless sudo and Docker

==> SSH alias and profile
SSH      alias example-profile -> dev@203.0.113.10 (~/.ssh/remote-dev-server-example-profile.conf)
Profile  ./deploy/host-profiles/example-profile.env

==> Install Coder and the workspace template
...
remote-dev install reconciled successfully

==> Doctor
OK   passwordless sudo
OK   Docker daemon
...
SKIP Cloudflare Tunnel (ssh-forward ingress publishes nothing)
...

==> Next steps
...
```

Если хотя бы одна проверка `doctor` не прошла, команда завершается с ненулевым
кодом; смотрите строки `FAIL`.

## Открыть панель

1. В одном терминале запустите проброс и оставьте его работать:

   ```bash
   bin/remote-dev --profile example-profile forward
   ```

   Пока используется минимальная топология, этот проброс нужен всем командам
   на Mac, которые обращаются к Coder (браузер, `workspace`, `codex-desktop`,
   `local-agent`, `credentials`).
2. Во втором терминале один раз получите вход владельца:

   ```bash
   bin/remote-dev --profile example-profile credentials reveal coder \
     --confirm-profile example-profile
   ```

   Откроется одноразовая страница на `127.0.0.1` с email владельца и
   сгенерированным паролем; через пять минут она очищается. Сохраните пароль в
   менеджере паролей.
3. Откройте <http://127.0.0.1:3080> и войдите.
4. Когда пароль сохранён и вход работает, удалите копию на сервере с помощью
   квитанции со страницы:

   ```bash
   bin/remote-dev --profile example-profile credentials finalize coder \
     --confirm-profile example-profile --confirm-stored \
     --receipt '<квитанция со страницы>'
   ```

## Первый workspace

В панели выберите **Create Workspace** → **Remote Dev Docker**, затем:

- **Workspace role:** *Development* (режим `remote`) или *Runner* (режим
  `local-agent`). Роль потом не меняется.
- **Docker capability:** *Core* (только Docker CLI) или *Containers*
  (внутренний демон Docker; нужен Sysbox, см. ниже).
- **Workspace size:** для начала подойдёт *Standard*.

Или с Mac, при запущенном пробросе:

```bash
bin/remote-dev --profile example-profile workspace create my-app \
  --role development --capability core --size standard
```

Ожидаемый результат: карточка workspace показывает **Running** и здоровый
агент. Сервис из workspace открывается на Mac так:
`bin/remote-dev --profile example-profile preview my-app 3000` — дождитесь
`READY` и откройте напечатанный адрес `http://127.0.0.1:...`.

Для workspace с `containers` один раз, в спокойное время, установите Sysbox
(первая установка перезапускает Docker на хосте; остановите workspace заранее):

```bash
bin/remote-dev --profile example-profile sysbox-install
```

## Восстановление

| Шаг | Симптом | Что делать |
| --- | --- | --- |
| Старт | `missing required tool: ...` | Установите указанный инструмент и повторите |
| Старт | `invalid profile name` | Используйте строчные буквы, цифры и `-` |
| Подготовка хоста | `cannot open the administrative SSH login` | Проверьте адрес и пользователя; передайте `--admin-key` с ключом провайдера; проверьте, что файрвол провайдера пропускает TCP/22 |
| Подготовка хоста | `supported baselines are Ubuntu 24.04 and 26.04` | Переустановите сервер с поддерживаемой версией |
| Подготовка хоста | `dev@<server> does not work after bootstrap` | Административное соединение остаётся открытым 15 минут; ошибка печатает команду `ssh -o ControlPath=...` для него. Проверьте `/home/dev/.ssh/authorized_keys` и `sshd -t`. Без соединения — веб-консоль провайдера |
| Любой следующий шаг | Вход под root отклоняется | Так и должно быть после bootstrap. Повторите ту же команду `quickstart`: рабочий вход `dev` пропускает bootstrap |
| SSH-алиас | `refusing non-regular SSH configuration state` | `~/.ssh/config` или фрагмент — ссылка или не обычный файл; замените обычным файлом и повторите |
| Профиль | `generated profile did not validate` | Обычно пересечение подсетей. Создайте профиль через `init` с явными `--egress-subnet`, `--egress-gateway`, `--control-subnet` и повторите `quickstart` (существующий профиль используется как есть) |
| Установка | Ошибка загрузки образа или пакета | Проверьте исходящий HTTPS с сервера и повторите `bin/remote-dev --profile <p> install`; команда идемпотентна |
| Установка | Шаблон не может получить провайдеры Terraform | Сервер не видит публичный реестр Terraform. Задайте `REMOTE_DEV_PROFILE_TERRAFORM_MIRROR=https://<зеркало>/` в профиле и повторите `install` |
| Doctor | Строка `FAIL` | Исправьте названный компонент и повторите `bin/remote-dev --profile <p> doctor` |
| Панель | Браузер не подключается к `127.0.0.1:3080` | Запустите `forward` и не закрывайте; если порт занят, пересоздайте профиль с другим `--local-port` |
| Пароль владельца | `reveal` не может войти | Проверьте, что `forward` работает, и повторите; копия на сервере хранится до `finalize` |
| Workspace | Создание не удалось | Сохраните лог сборки Coder, запустите `doctor` и повторите. Для `containers` сначала нужен `sysbox-install` |

Чтобы начать заново только на стороне Mac, удалите
`deploy/host-profiles/<profile>.env` и `~/.ssh/remote-dev-server-<profile>.conf`
и строку `Include` для него в `~/.ssh/config`. Набор никогда сам не удаляет
данные на сервере; чтобы начать заново на сервере, переустановите его.

## Ограничения

Это свойства продукта, а не конкретной установки. Полный список:
[limitations.md](../limitations.md).

- **Один доверенный оператор**, без изоляции между пользователями. Provisioner
  Coder имеет Docker-сокет хоста (эквивалент root); шаблоны должны быть
  доверенными. У пользователя `dev` sudo без пароля.
- Клиент — **только macOS**; сервер — **Ubuntu 24.04 или 26.04 на x86_64**.
- **Один сервер на профиль, без высокой доступности.**
- Лимиты CPU, RAM и процессов жёсткие; **диск без квот** — `audit` лишь
  сравнивает использование с мягким бюджетом. Постоянного мониторинга и
  внешних оповещений нет.
- **Резервное копирование вне хоста дано как механизм**: таймер по умолчанию
  выключен, восстановление на чистом хосте в рамках этого выпуска не
  репетировалось.
- `local-agent` **не может запретить локальный shell агента**: он направляет
  нагрузку на runner инструкциями и инструментами и доказывает удалённое
  выполнение квитанциями. Runner — без `.git`, передача — односторонняя.
- **Смена топологии** уже установленного сервера — ручная, не
  отрепетированная смена идентичности.
- Адаптер Codex необязателен и привязан к конкретной версии Codex CLI в образе.
- Предоставляется «как есть», без SLA.

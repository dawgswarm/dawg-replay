# the-internet-task

Демо задачи без таблицы на учебном сайте https://the-internet.herokuapp.com/dynamic_loading/2.
Скрипт открывает страницу и сохраняет скриншот; в режиме «Нажать Start» после подтверждения
нажимает кнопку, ждёт «Hello World!» и сохраняет второй скриншот.

Что видно на демо:

- выбор режима с понятными подписями (`enum` с `{ value, title }`);
- вопрос-подтверждение перед действием;
- скриншоты миниатюрами на экране запуска, главный файл итога («Открыть результат») и ссылка на страницу.

## Запуск

    dawg-replay validate examples/the-internet-task/script.py
    dawg-replay add examples/the-internet-task/script.py
    dawg-replay run the-internet-task --set mode=act --follow

Пока kit 1.3 не опубликован на PyPI, приложению нужен локальный kit:

    dawg-replay config set dev_kit_source "$PWD/packages/dawg-replay-kit"

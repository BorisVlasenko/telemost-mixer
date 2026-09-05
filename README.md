# Telemost Mixer

Компактная панель (и трей) для одновременной работы с **4 окнами Яндекс Телемоста** на Linux (KDE + PipeWire).

Глушит микрофон и звук **на уровне PipeWire** (`pactl`), не кликая по UI Телемоста. Окна браузера можно свернуть — управление остаётся на панели.

## Возможности

- 4 канала: подпись ученика, ★ (говорить только в этот канал), копирование ссылки, закрытие одного канала, мик / звук
- ★ выключает микрофоны остальных каналов, звук всех продолжает идти
- Запуск / закрытие всех окон Яндекс.Браузера (`--app` + отдельные профили)
- Камера отключена флагом `--disable-video-capture` (меньше конфликтов устройств)
- Трей: показать панель, править `channels.json`, перечитать ссылки

## Требования

- Linux, PipeWire / PulseAudio (`pactl`)
- Яндекс.Браузер (`yandex-browser` в `PATH`)
- Python 3 + PyQt6 (`python3-pyqt6` в Debian/Ubuntu/Kubuntu)

## Установка

```bash
git clone https://github.com/BorisVlasenko/telemost-mixer.git
cd telemost-mixer
cp channels.example.json channels.json
# пропиши ссылки на встречи в channels.json
python3 mixer.py
```

Опционально: скопируй `telemost-mixer.desktop` в `~/.local/share/applications/` и поправь путь к `mixer.py`.

Профили браузера: `~/.local/share/telemost-mixer/ch1` … `ch4`.

## Важно

- Mute идёт через PipeWire: в UI Телемоста иконка микрофона может остаться «вкл».
- Имя на встрече Телемост часто просит вручную — панель его не подставляет.
- `channels.json` в git не коммитится (личные ссылки). В репозитории только `channels.example.json`.

## Лицензия

MIT

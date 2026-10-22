# Выбор Jev-like модели

В SPAS уже реализован scorer **bespokelabs/Bespoke-Nimble-9B**:
[описание Gateway](../../../llm/LLM.md#модели-решений-jev-like-nimble),
[реализация](../../../llm/gateway/colab/nimble_runtime.py).
Это модель типизированных решений, не Chat Completions. Официальный scorer закреплён
на commit `dcfdbd9a64f0d869f658d7a72f1beaee32737773`; требуется CUDA runtime,
веса 9B занимают около 18 GB без квантования. Для запуска используйте существующую
настройку backend/Colab в SPAS, затем его логический маршрут в `SAFETY_MODEL`.
В этой облачной среде нет CUDA, развёрнутого runtime и настроенного Gateway для inference;
веса не загружались и качество реальной модели не измерялось.

Analyzer поддерживает `SAFETY_COMMAND_API=decisions`: три System One Choice вопроса
(decision, risk, основной factor) с полной командой и контекстом в `state`.
Gateway преобразует их в native `context`/`schema` для Nimble. Ответ проверяется:
все вопросы и варианты, вероятности/их сумма, confidence, model route,
согласованность decision/risk/factor. Refusal или повреждённый ответ дают fail-closed DENY.
Reason составляется из выбранных категорий, произвольный текст модель не генерирует.
Вероятности scorer не являются измеренной вероятностью ущерба.

Исходный контекст не обрезается: если полный prompt со схемой превышает лимит
checkpoint, runtime отклоняет запрос. Бюджет Analyzer остаётся 4 секунды по умолчанию
(максимум 4.5): медленный scorer отражается в error rate, а не скрывается повторами.
Параметры Chat Completions (temperature/max_tokens/output_mode) к native Choice не применяются.
Fallback на чат или другой формат отсутствует. [Оценка](evaluation.md) фиксирует различие
между контрольным прогоном транспорта и inference реальных весов.

Остальные варианты для сравнительной оценки:

| Вариант | Применение | Что проверить |
| --- | --- | --- |
| Модель общего назначения через существующий SPAS Gateway | Контекстный анализ argv и JSON-решения | Доступный аккаунту маршрут, формат, задержка, стоимость/квота и датасет |
| OpenAI Moderation API | Дополнительная проверка категорий контента | Это не проверка прав/последствий команд и не замена Rule-Based Checker |
| Локальная Llama Guard | Кандидат для native safe/unsafe | Лицензия, native chat template, язык, GPU/память, длина контекста, метки |

Готовые guard-модели обычно обучены безопасности диалогов, поэтому безопасность
системных команд и разрешения инфраструктуры нужно проверять отдельно на датасете.

Исходные точки: [OpenAI Moderation](https://platform.openai.com/docs/guides/moderation),
[Llama Guard 3](https://huggingface.co/meta-llama/Llama-Guard-3-8B).
Это варианты для проверки, не протестированные/одобренные в этой среде модели.
Публичность весов не означает отсутствие затрат на GPU/runtime.

Для сравнения с чат-моделью задайте `SAFETY_COMMAND_API=chat_completions`,
Gateway URL/client key/model route и явный
`SAFETY_COMMAND_OUTPUT_MODE=json|label`. Для label native template и метки модели
должны совпасть с адаптером; иначе доработайте `command_payload`/`parse_command_output`
и повторите benchmark. Не интерпретируйте произвольную подстроку safe как разрешение.
Закрепите revision весов и единственный backend маршрута на время оценки.

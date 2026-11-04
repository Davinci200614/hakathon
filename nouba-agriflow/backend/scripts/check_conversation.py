"""Synthetic understanding benchmark: no database writes or WhatsApp sends."""
import asyncio
import time
from app import config, extraction
from app.ai import AIError
from app.understanding import selected_provider, selected_model, understand


async def main():
    provider = selected_provider()
    model = selected_model()
    print(f'provider={provider} model={model} budget={config.CONVERSATION_TIMEOUT_SECONDS}s')
    examples = [
        ('عندي ضيعة وباغي ناس يعاونوني فالجني، واش تقدر تلقا ليا شي حد؟', 'search_workers'),
        ('بغيت تراكتور يحرث جوج هكتارات فمكناس غدا', 'machine'),
    ]
    failures = 0
    for text, expected in examples:
        started = time.monotonic()
        try:
            result = await understand(text, {'today': extraction.casablanca_today().isoformat(),
                'timezone': 'Africa/Casablanca', 'active_task': 'machine',
                'draft': {'machine_type': 'tractor', 'location': 'Fes'}, 'pending_field': None,
                'recent_turns': []})
            ok = result.task == expected
            failures += not ok
            fields = sorted(result.facts.model_dump(exclude_none=True))
            print(f'expected={expected} actual={result.task} fields={fields} latency={time.monotonic()-started:.2f}s ok={ok}')
        except AIError as exc:
            failures += 1
            print(f'expected={expected} error={exc.code} latency={time.monotonic()-started:.2f}s')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

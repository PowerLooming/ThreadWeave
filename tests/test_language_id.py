# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the deterministic language detector (language_id.py)."""

import pytest

from threadweave.language_id import SUPPORTED_LANGUAGES, identify

SAMPLES = {
    "en": "After three weeks of load testing we decided to move the session cache to Redis because the locks were the bottleneck.",
    "no": "Etter tre uker med lasttesting besluttet vi å flytte sesjonscachen til Redis fordi låsene ble flaskehalsen ved høy last.",
    "da": "Efter tre ugers lasttest besluttede vi at flytte sessionscachen til Redis, fordi låsene blev flaskehalsen.",
    "sv": "Efter tre veckor av lasttestning beslutade vi att flytta sessionscachen till Redis eftersom låsen blev flaskhalsen.",
    "de": "Nach drei Wochen Lasttest haben wir entschieden, den Sitzungscache auf Redis zu verschieben, weil die Sperren der Engpass waren.",
    "nl": "Na drie weken lasttesten hebben we besloten de sessiecache naar Redis te verplaatsen omdat de sloten het knelpunt waren.",
    "fr": "Après trois semaines de tests de charge nous avons décidé de déplacer le cache de session vers Redis car les verrous étaient le goulot.",
    "es": "Después de tres semanas de pruebas de carga decidimos mover la caché de sesión a Redis porque los bloqueos eran el cuello de botella.",
    "it": "Dopo tre settimane di test di carico abbiamo deciso di spostare la cache di sessione su Redis perché i blocchi erano il collo di bottiglia.",
    "pt": "Depois de três semanas de testes de carga decidimos mover o cache de sessão para o Redis porque os bloqueios eram o gargalo.",
    "pl": "Po trzech tygodniach testów obciążenia zdecydowaliśmy się przenieść pamięć podręczną sesji do Redis, bo blokady były wąskim gardłem.",
    "fi": "Kolmen viikon kuormitustestien jälkeen päätimme siirtää istuntovälimuistin Redisiin, koska lukot olivat pullonkaula.",
    "tr": "Üç haftalık yük testinden sonra oturum önbelleğini Redis'e taşımaya karar verdik çünkü kilitler darboğazdı.",
    "ru": "После трёх недель нагрузочного тестирования мы решили перенести кеш сессий в Redis, потому что блокировки были узким местом.",
    "uk": "Після трьох тижнів навантажувального тестування ми вирішили перенести кеш сесій у Redis, бо блокування були вузьким місцем.",
    "ar": "بعد ثلاثة أسابيع من اختبار الحمل قررنا نقل ذاكرة التخزين المؤقت للجلسة إلى ريديس لأن الأقفال كانت عنق الزجاجة.",
    "hi": "तीन हफ्तों के लोड परीक्षण के बाद हमने सेशन कैश को रेडिस में ले जाने का फैसला किया क्योंकि लॉक बाधा थे।",
    "zh": "经过三周的负载测试，我们决定把会话缓存迁移到 Redis，因为数据库锁成了瓶颈。",
    "ja": "3週間の負荷テストの後、データベースロックがボトルネックだったため、セッションキャッシュをRedisに移すことにしました。",
    "ko": "3주간의 부하 테스트 후 데이터베이스 잠금이 병목이어서 세션 캐시를 Redis로 옮기기로 했습니다.",
}


class TestDetection:
    @pytest.mark.parametrize("expected,text", sorted(SAMPLES.items()))
    def test_expected_language(self, expected, text):
        guess = identify(text)
        assert guess.language == expected, f"{guess} for {text[:40]}"

    def test_every_sample_language_is_declared_supported(self):
        for lang in SAMPLES:
            assert lang in SUPPORTED_LANGUAGES

    def test_norwegian_beats_danish_on_norwegian_markers(self):
        guess = identify(SAMPLES["no"])
        assert guess.language == "no"
        assert guess.runner_up == "da"  # the hard pair, reported honestly

    def test_ukrainian_cyrillic_is_not_russian(self):
        assert identify(SAMPLES["uk"]).language == "uk"
        assert identify(SAMPLES["ru"]).language == "ru"

    def test_han_with_kana_is_japanese_not_chinese(self):
        assert identify(SAMPLES["ja"]).language == "ja"
        assert identify(SAMPLES["zh"]).language == "zh"


class TestConfidence:
    def test_empty_input_yields_nothing(self):
        for text in ("", "   ", "\n"):
            assert identify(text).language == ""

    def test_a_single_ambiguous_word_is_below_the_threshold(self):
        # "ok" and "ja" are genuinely ambiguous and too short to decide with.
        assert identify("ok", min_confidence=0.25).language == ""
        assert identify("ja", min_confidence=0.25).language == ""

    def test_confidence_rises_with_a_clearer_signal(self):
        weak = identify("Vi besluttet å bytte til Postgres i går.").confidence
        strong = identify(SAMPLES["en"]).confidence
        assert strong > weak > 0

    def test_norwegian_danish_tie_reports_no_confidence(self):
        # "vi" and "skal" exist in both profiles: the detector must not guess.
        assert identify("Vi skal bytte cache i dag.").confidence == 0.0

    def test_never_returns_the_choice_escape_hatch(self):
        # "other" is a Choice option for the model, not a detector answer.
        for text in SAMPLES.values():
            assert identify(text).language != "other"

    def test_pure_numbers_and_codes_yield_nothing(self):
        assert identify("2026-09-26 14:57 PR id#355960 v2.13.1").language == ""

    def test_short_norwegian_message_still_routes(self):
        guess = identify("Vi besluttet å bytte til Postgres i går.", min_confidence=0.25)
        assert guess.language == "no"

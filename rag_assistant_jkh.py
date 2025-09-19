import os
import re
import warnings
import numpy as np
import faiss
import json
import random
import time
from typing import List, Dict, Tuple, Optional, Type, Any
from pathlib import Path
from sentence_transformers import SentenceTransformer
import nltk
from nltk.tokenize import sent_tokenize
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
import torch
from torch.cuda.amp import autocast
import gradio as gr
from ddgs import DDGS

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

warnings.filterwarnings('ignore')

try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt', quiet=True)

STOP_WORDS = {
    "добрый", "день", "прошу", "пожалуйста", "меры", "примите", "здравствуйте",
    "спасибо", "вечер", "утро", "хочу", "напоминаю", "уведомляю"
}

# ---------------------------
# Загрузка моделей
# ---------------------------

print("🧠 Загрузка моделей...")

print("📥 Загрузка модели ViktorZver/FRIDA...")
embedding_model = SentenceTransformer("ViktorZver/FRIDA", device=str(device))
print("✅ FRIDA загружена")

print("📥 Загрузка токенизатора YandexGPT-5-Lite...")
tokenizer = AutoTokenizer.from_pretrained("yandex/YandexGPT-5-Lite-8B-instruct")
model_name = "yandex/YandexGPT-5-Lite-8B-instruct"

if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

def estimate_tokens(text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False))

print("📥 Загрузка YandexGPT-5-Lite-8B-instruct в 4-bit...")
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.float16,
    bnb_4bit_use_double_quant=True,
)

try:
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        #quantization_config=bnb_config,
        device_map="auto",
        torch_dtype=torch.float16,
        trust_remote_code=False,
        attn_implementation="sdpa"
    )
    print(f"✅ LLM загружена в 4-bit на: {device}")
except Exception as e:
    print(f"❌ Ошибка загрузки модели: {e}")
    raise

# ---------------------------
# Загрузка данных
# ---------------------------

CHUNKS_PATH = "/kaggle/input/jkh-data/document_chunks.json"
INDEX_PATH  = "/kaggle/input/jkh-data/faiss_index.bin"

if not os.path.exists(CHUNKS_PATH) or not os.path.exists(INDEX_PATH):
    print(f"⚠️ Файлы данных не найдены: {CHUNKS_PATH}, {INDEX_PATH}")
    chunks_data = []
    index = None
else:
    print("📥 Загрузка чанков...")
    with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
        chunks_data = json.load(f)
    print(f"✅ Чанков загружено: {len(chunks_data)}")

    print("📥 Загрузка FAISS-индекса...")
    index = faiss.read_index(INDEX_PATH)
    print(f"✅ Индекс загружен: {index.ntotal} векторов")


# ---------------------------
# Базовый класс агента
# ---------------------------

class RAGAgent:
    def __init__(self, name: str, keywords: List[str]):
        self.name = name
        self.keywords = [kw.lower() for kw in keywords]
        self.feedback_data = []
        self.confidence_threshold = 0.7
        self.load_feedback()

    """def matches(self, query: str) -> bool:
        q = query.lower()
        # Извлекаем только целые слова
        words = set(re.findall(r'\b[а-яёa-z0-9]+\b', q))
        return any(kw in words for kw in self.keywords)"""
    def matches(self, query: str) -> bool:
        q = query.lower()
        # Проверяем, содержит ли запрос ЛЮБОЕ из ключевых слов как подстроку
        # Это делает систему устойчивой к опечаткам, склонениям и частичным совпадениям
        return any(kw in q for kw in self.keywords)
       
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        raise NotImplementedError("Каждый агент должен реализовать свой _build_prompt")

    def get_role_instruction(self, role: str) -> str:
        base = {
            "житель": "Ответ ориентирован на жителя. Давайте пошаговые действия с ссылками на НПА.",
            "исполнитель": "Ответ ориентирован на УК/ТСН. Включайте судебную практику и процедуры.",
            "смешанная": "Разделите ответ на две части: для жителя и для исполнителя."
        }
        return base.get(role, base["смешанная"])

    # ---- Обучение агента ----
    def add_feedback(self, query: str, ideal_answer: str, rating: float = 1.0):
        if rating >= 0.8:
            self.feedback_data.append({
                "query": query,
                "ideal_answer": ideal_answer,
                "rating": rating,
                "timestamp": time.time()
            })
        self._save_feedback()

    def _save_feedback(self):
        feedback_file = f"agent_feedback_{self.name.replace(' ', '_')}.json"
        with open(feedback_file, "w", encoding="utf-8") as f:
            json.dump(self.feedback_data, f, ensure_ascii=False, indent=2)

    def load_feedback(self):
        feedback_file = f"agent_feedback_{self.name.replace(' ', '_')}.json"
        if os.path.exists(feedback_file):
            try:
                with open(feedback_file, "r", encoding="utf-8") as f:
                    self.feedback_data = json.load(f)
            except:
                self.feedback_data = []

    def improve_prompt_from_feedback(self) -> str:
        if len(self.feedback_data) < 3:
            return ""
        examples = random.sample(self.feedback_data, min(3, len(self.feedback_data)))
        instruction = "\n\nНа основе успешных примеров, улучши стиль и структуру ответа:\n"
        for ex in examples:
            instruction += f"Вопрос: {ex['query']}\nОтвет: {ex['ideal_answer']}\n---\n"
        return instruction

    # ---- Мультиагентность: запрос к другому агенту ----
    def consult_other_agent(self, query: str, rag_system) -> str:
        """Запрашивает контекст у другого агента через MetaAgent"""
        other_agent = rag_system.meta_agent.route(query, exclude_agent=self)
        if other_agent:
            print(f"🤝 {self.name} запрашивает помощь у {other_agent.name}")
            context = rag_system.generate_context_for_agent(query, other_agent, rag_system.detect_user_role(query))
            return f"\n\n[Консультация от агента '{other_agent.name}']: {context}\n"
        return ""


# ---------------------------
# Конкретные агенты
# ---------------------------

class TariffAgent(RAGAgent):
    def __init__(self):
        super().__init__("Тарифы и начисления", [
            "тариф", "начисл", "оплата", "сумма", "одн", "гвс", "ипу", "кубометр", "стоимость", "перерасчет", "повышающий коэффициент",
            "свидетельство о поверке", "акт поверки", "результаты поверки", "перерасчёт после поверки", "срок поверки истёк", 
            "истёк срок поверки", "акт сверки", "стоимость акта", "плата за акт", "услуга перед продажей", "документ перед продажей", 
            "продажа квартиры", "акт сверки счетчиков", "плановое отключение", "профилактические работы", "14 суток",
            "отпуск", "командировка", "временное отсутствие", "уехал", "перерасчет за отсутствие", "документы для перерасчета",
            "платёжка", "квитанция", "не совпадает сумма", "долг в квитанции", "задвоили оплату", "комиссия банка", "сумма ОДН", 
            "почему за лифт", "переплата", "не пришла оплата", "где долг", "неправильный тариф", "не соответствует региональному", 
            "повышение тарифа", "обоснование тарифа", "расчет по нормативу", "расчет по показаниям", "расчет по среднему", 
            "перерасчет по акту", "перерасчет по заявлению", "комиссия за оплату"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra} {web_results} \n\n"
            f"Ты — эксперт по тарифам ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Основной документ: ПП РФ №354 «О предоставлении коммунальных услуг» \n"
            f"- Для оспаривания начислений: ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан» \n"
            f"- Для расчётов с ИПУ/ОДПУ: Приложение №2 к ПП РФ №354 \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Объясни, что задержка платежей через банк может составлять до 3 рабочих дней.\n\n"
            f"Разъясни, если вопрос об оплате лифта, что лифт оплачивается всеми собственниками, так как это общее имущество (ЖК РФ ст.36)"
            f"ДОПОЛНИ инструкцию по перерасчету: \n\n"
            f"4. Всегда уточняй: 'Управляющая организация обязана произвести перерасчет в течение 5 рабочих дней с момента получения заявления и подтверждающих документов (ПП РФ №354, п. 106).' \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Если вопрос о **перерасчёте за период планового отключения** (например, горячей воды): \n\n"
            f"   - Начни с: 'Согласно п. 4 Приложения № 1 к ПП РФ №354, отключение при профилактике не должно превышать 14 суток.' \n"
            f"   - Объясни простым языком: 'Перерасчет за плановое отключение не производится, так как это предусмотрено законом и не считается нарушением качества услуги. Это не ошибка, а стандартная практика.' (ПП РФ №354, п. 98). \n"
            f"   - Уточни: 'Перерасчет возможен ТОЛЬКО если отключение превысило 14 дней или если температура воды была ниже нормы (менее 60°C) во время подачи.' (ПП РФ №354, п. 99). \n\n"
            f"2. Если вопрос о **повышающем коэффициенте** (например, после истечения поверки ИПУ): \n\n"
            f"   - Четко укажи: 'Повышающий коэффициент 1.5 применяется согласно п. 81(12) ПП РФ №354, если не переданы показания или истек срок поверки прибора.' \n"
            f"   - Укажи, как это исправить: 'Для отмены коэффициента предоставьте акт о поверке прибора учета в управляющую компанию.' \n\n"
            f"3. Всегда структурируй ответ: \n\n"
            f"   - Как рассчитывается плата? \n"
            f"   - Какие нормативы применяются? \n"
            f"   - Как оспорить начисление? (ФЗ №59-ФЗ, ст. 8 — срок ответа 30 дней) \n\n"
            f"4. Если вопрос о **перерасчете при временном отсутствии** (отпуск, командировка): \n\n"
            f"   - Укажи: 'Перерасчет возможен только по услугам, рассчитываемым по нормативу, и при отсутствии более 5 дней.' (ПП РФ №354, п. 86) \n"
            f"   - Перечисли: 'Необходимо подать заявление + документы (билеты, справка, путевка) до отъезда или в течение 30 дней после возвращения.' \n"
            f"   - Предупреди: 'Если в квартире установлен ИПУ, перерасчет не производится — плата начисляется по фактическим показаниям.' \n"
            f"ОБЯЗАТЕЛЬНО выполни следующую проверку: \n\n"
            f"5. Если вопрос касается **срока оплаты ЖКУ** или **срока подачи документов** (например, справок об отсутствии): \n\n"
            f"   - Начни ответ с прямой цитаты из закона: 'Согласно ст. 155 Жилищного кодекса РФ, плата за жилое помещение и коммунальные услуги вносится ежемесячно до десятого числа месяца, следующего за истекшим месяцем, если иной срок не установлен договором управления или решением общего собрания.' \n"
            f"   - Для справок об отсутствии: 'Справки о временном отсутствии должны быть предоставлены до начала периода отсутствия или не позднее 30 дней после его окончания (ПП РФ №354, п. 86).' \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class NormativeAgent(RAGAgent):
    def __init__(self):
        super().__init__("Нормативные документы", [
            "закон", "фз", "пп рф", "норматив", "право", "регламент", "жилищный кодекс", "закон о жкх", 
            "какой норматив", "обязанности УК", "права жильцов", "где прописано", "сошлитесь на закон", "по закону",
            "разъяснения Минстроя", "письма Ростехнадзора", "разъяснения ФАС", "методические рекомендации", "обзор практики",
            "постановление Пленума ВС РФ", "определение Конституционного Суда РФ", "международные конвенции", "европейская практика"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        web_results = self._perform_web_search(summary)
        extra = self.improve_prompt_from_feedback()
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra} {web_results} \n\n"
            f"Ты — юрист по ЖКХ. Ответь строго по документам: \n\n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора.\n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера.\n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Если вопрос касается ТСЖ/УК — поясни, что ТСЖ создаётся бессрочно, но может быть ликвидировано решением собрания собственников. \n\n"
            f"Всегда приводи ссылки на Жилищный кодекс РФ, Постановление №354 и региональные акты \n\n"
            f"- Основные документы: Жилищный кодекс РФ, ПП РФ №354, №491, №416, Гражданский кодекс РФ \n\n"
            f"- Цитируй статьи и пункты дословно. \n"
            f"- Указывай полные названия документов (например: Постановление Правительства РФ от 06.05.2011 №354). \n"
            f"- Не давай советов без ссылок. \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class TechnicalAgent(RAGAgent):
    def __init__(self):
        super().__init__("Технические регламенты", [
            "снип", "гост", "температура", "шум", "лифт", "давление", "норма",
            "полотенцесушитель", "не греет", "не работает", "не запустился", "холодный",
            "труба", "батарея", "радиатор", "циркуляция", "стояк", "протечка", "засор",
            "давление", "напор", "слабый напор", "нет горячей воды", "нет холодной воды",
            "холодно в квартире", "жарко", "стояк", "батарея", "воздух в батарее", "температура воды", 
            "греет плохо", "замер температуры", "санпин", "норма отопления", "гудит лифт", "сломался лифт", 
            "шум в подвале", "воняет в подъезде", "нормативная температура", "перегрев воды", "технические условия", "трехтрубная система", 
            "давление на вводе", "регресс к УО", "циркуляционный насос", "ИТП", "тепловой пункт", "воздух в системе", "завоздушивание", 
            "гидравлический удар"
        ])

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra} \n\n"
            f"Ты — инженер ЖКХ. Ответь технически точно по следующим нормативам: \n\n"
            f"- Основной документ: Постановление Госстроя РФ №170 «Правила технической эксплуатации жилищного фонда» \n"
            f"- СНиП 2.04.01-85 — внутренний водопровод и канализация \n"
            f"- ГОСТ Р 51617-2000 — технические условия на услуги ЖКХ \n"
            f"- СанПиН 2.1.2.2645-10 — микроклимат в помещениях \n\n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Дай пошаговый алгоритм: если в квартире холодно → вызвать УК, составить акт, пригласить соседей как свидетелей \n\n"
            f"Если вопрос об лифте — напомни, что это часть общего имущества, а аварийные ситуации фиксируются актом с УК и подрядчиком \n\n"
            f"ОБЯЗАТЕЛЬНО включи в ответ: \n\n"
            f"- Указание на допустимую погрешность измерений. Например: 'Нормативная температура горячей воды — не ниже 60°C (ПП РФ №354, п. 30). Допустимая погрешность измерения — ±3°C (ГОСТ Р 55964-2014).' \n"
            f"- Если пользователь сообщает о нарушении, уточни: 'Для фиксации нарушения необходимо провести измерение в присутствии представителя УК с использованием поверенного прибора.' \n\n"
            f"Структурируй ответ: \n\n"
            f"- Приведи нормы из СНиП/ГОСТ (с номерами разделов). \n"
            f"- Укажи допустимые отклонения (например: температура в жилом помещении — не ниже +18°C, СанПиН 2.1.2.2645-10).\n"
            f"- Если нарушение — как зафиксировать? (акт осмотра, фото, подпись УК/ТСЖ) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class MeterAgent(RAGAgent):
    def __init__(self):
        super().__init__("Приборы учёта", [
            "счётчик", "показания", "пу", "ипу", "одпу", "поверка", "замена", "реальные показания",
            "счётчик", "опломбировка", "поверка", "замена счётчика", "не работает счётчик", "подать показания", 
            "куда передать", "ошибка в показаниях", "кто должен менять","акт обследования", "невозможность установки", 
            "техническая невозможность", "демонтаж счетчика", "опломбировка счетчика", "электронный счетчик", "механический счетчик", 
            "дистанционная передача", "автоматическая передача", "срок поверки"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results}\n\n"
            f"Ты — специалист по ПУ. Ответь пошагово на основе: \n\n"
            f"- ПП РФ №354 (гл. VII — приборы учета) \n"
            f"- ФЗ №102-ФЗ «Об обеспечении единства измерений» (поверка) \n"
            f"- ПП РФ №554 — правила функционирования ресурсоснабжающих организаций \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Разъясни, что опломбировка счётчика чаще всего бесплатна, если иное не указано в договоре \n\n"
            f"Укажи, что поверка проводится раз в установленный межповерочный интервал, а при неисправности собственник обязан заменить счётчик \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующую проверку: \nn"
            f"1. Если вопрос касается невозможности установки счетчика, ТЫ ОБЯЗАН уточнить: \n\n"
            f"   - 'Техническая невозможность установки должна быть подтверждена актом обследования, составленным УК совместно с РСО (ПП РФ №354, п. 81(10)). Без такого акта предполагается, что установка возможна.' \n"
            f"   - 'Даже при технической невозможности, обязанность по оплате коммунальных услуг сохраняется. Расчет производится по нормативу с применением повышающего коэффициента 1.5 (ПП РФ №354, п. 42).' \n\n"
            f"Ответь: \n\n"
            f"- Как передать показания? (личный кабинет, ЕПД, приложение, ПП РФ №354 п. 31) \n"
            f"- **Пошагово объясни, как рассчитывается плата при неисправности ИПУ:** \n"
            f"   1. Первые 3 расчетных периода — плата рассчитывается исходя из среднемесячного объема потребления, определенного по показаниям ИПУ за предыдущие 6 месяцев (ПП РФ №354, п. 59). \n"
            f"   2. Начиная с 4-го расчетного периода — плата рассчитывается исходя из норматива потребления с применением повышающего коэффициента 1.5 (ПП РФ №354, п. 42, п. 81(12)). \n"
            f"   3. Как только ИПУ будет отремонтирован или заменен, производится перерасчет за период, когда расчет велся по среднему/нормативу. \n\n"
            f"- Кто оплачивает поверку? (собственник — ФЗ №102-ФЗ, ст. 13) \n"
            f"- Как оспорить начисления, если показания не учтены? (заявление + акт, ПП РФ №354, п. 86) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class DebtAgent(RAGAgent):
    def __init__(self):
        super().__init__("Задолженности", [
            "долг", "задолженность", "пени", "неуплата", "оплата", "рассрочка", "завышенное начисление",
            "задолженность", "пени", "рассрочка", "штрафы", "списание долга", "оплатил но долг", "почему долг", 
            "коллекторы", "взыскание", "суд за долг", "суд", "неустойка", "моральный вред", "взыскание долга", "исковое заявление", 
            "приказное производство", "приостановка коммунальных услуг", "ограничение услуги", "отключение за неуплату", "запрет на выезд",
            "ключевая ставка", "ставка 9.5%", "ограничение ставки", "ставка цб", "9.5 процентов", "фз 44-фз",
            "постановление 474", "постановление 1681", "постановление 2382", "постановление 329", "до 2027 года","пеня"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        # web_results = self._perform_web_search(summary)  # Закомментировано, чтобы не мешать основному контексту
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra} \n\n"
            f"Ты — юрист по долгам ЖКХ. Ответь строго по нормативам, используя ТОЛЬКО информацию из контекста. \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующую проверку: \n"
            f"1. Если вопрос касается **расчета пени**: \n"
            f"   - **НЕ ИСПОЛЬЗУЙ устаревшие формулы. Найди в контексте самый последний нормативный акт, регулирующий размер пени.** \n"
            f"   - **Прямо укажи АКТУАЛЬНУЮ формулу расчета пени, как она прописана в найденном акте.** \n"
            f"   - **Обязательно поясни, что такое 'Применяемая ставка' и укажи ее значение на текущую дату, ссылаясь на конкретный нормативный акт.** \n"
            f"2. Перед тем как давать любые рекомендации по долгу, ТЫ ОБЯЗАН проверить срок исковой давности. \n"
            f"   - Напомни пользователю: 'Срок исковой давности по долгам ЖКХ составляет 3 года (ст. 196 ГК РФ). Если с момента последнего платежа или признания долга прошло более 3 лет, долг может быть оспорен в суде.' \n"
            f"   - Уточни: 'Срок давности применяется только по заявлению должника. Если вы не заявили о пропуске срока в суде, долг подлежит взысканию.' \n\n"
            f"3. Если вопрос касается **ограничения ставки 9,5%** при расчете пеней и в контексте есть ссылка на ФЗ №44-ФЗ или ПП РФ №474: \n"
            f"   - Начни с: 'Ограничение ставки для расчета пеней установлено Федеральным законом от 08.03.2015 № 44-ФЗ и Постановлением Правительства РФ от 26.03.2022 №474 (с продлениями).' \n"
            f"   - Поясни: 'Согласно этим актам, для расчета пеней применяется значение, не превышающее 9,5% годовых, до 1 января 2027 года.' \n\n"
            f"Ответь: \n"
            f"- Как списать долг? (по решению суда, банкротство, истечение срока давности) \n"
            f"- Размер пени? (укажи АКТУАЛЬНУЮ формулу из контекста и поясни применяемую ставку) \n"
            f"- Срок исковой давности? (3 года, ст. 196 ГК РФ) \n"
            f"- Как оспорить завышенное начисление? (перерасчёт по акту, ПП РФ №354 п. 106) \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class DisclosureAgent(RAGAgent):
    def __init__(self):
        super().__init__("Раскрытие информации", [
            "раскрытие", "гис жкх", "отчёт", "информация", "доступ", "публичный",
            "личный кабинет", "телеграмм канал", "информационный стенд","гис жкх", "отчёт УК", 
            "раскрытие информации", "протоколы собраний", "где посмотреть отчёт", "план работ", "смета расходов",
            "не публикуют отчеты", "нет информации в ГИС ЖКХ", "не отвечают на запросы", "отказ в предоставлении информации",
            "доступ к документам", "копия договора", "протокол собрания", "финансовый отчет", "бюджет дома",
            "загружать", "грузить", "публиковать", "размещать", "сроки загрузки", "сроки размещения", "обновлять информацию", 
            "когда публиковать", "когда размещать", "загрузить", "грузить", "разместить", "опубликовать", "обновить",
            "когда появится в гис жкх", "почему в гис жкх нет данных", "сроки загрузки в гис жкх"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по раскрытию информации. Ответь по: \n\n"
            f"- ФЗ №209-ФЗ «О раскрытии информации в ЖКХ» \n"
            f"- ПП РФ №731 — стандарт раскрытия информации \n"
            f"- Приказ Минстроя РФ №48/414 — форма отчётов \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Объясни, что вся информация об УК должна быть доступна в ГИС ЖКХ и на официальном сайте управляющей компании \n\n"
            f"ОБЯЗАТЕЛЬНО структурируй ответ по типам информации и укажи точные сроки: \n\n"
            f"- Отчет об исполнении управляющей организацией договора управления: не позднее 10 числа месяца, следующего за отчетным (ПП РФ №731, п. 10). \n"
            f"- Сведения о выполняемых работах по содержанию и ремонту общего имущества: ежемесячно, не позднее 10 числа следующего месяца (ПП РФ №731, п. 11). \n"
            f"- Сведения о ценах (тарифах) на коммунальные ресурсы: в течение 3 рабочих дней с момента их установления (ПП РФ №731, п. 13). \n\n"
            f"Ответь: \n\n"
            f"- Где найти отчёт? (ГИС ЖКХ → раздел «Управляющая организация» → «Отчёты») \n"
            f"- Сроки размещения? (не позднее 10 числа месяца, следующего за отчётным — ПП РФ №731) \n"
            f"- Что делать, если информация не размещена? (жалоба в ГЖИ, через ГИС ЖКХ или ФЗ №59-ФЗ) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class IoTAgent(RAGAgent):
    def __init__(self):
        super().__init__("IoT и мониторинг", [
            "датчик", "утечка", "температура", "iot", "умный", "мониторинг", "авария","датчик протечки", "датчик задымления", 
            "умный термостат", "умный счетчик воды", "умный счетчик тепла", "интеграция с умным домом", "уведомления в телеграм", 
            "уведомления в whatsapp", "API для интеграции", "вебхуки"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — инженер IoT в ЖКХ. Ответь технически по: \n\n"
            f"- ФЗ №152-ФЗ «О персональных данных» (если сбор данных с жильцов) \n"
            f"- ФЗ №187-ФЗ «О безопасности критической информационной инфраструктуры» \n"
            f"- ГОСТ Р 57580 — системы умного дома \n"
            f"- Регламенты Ростехнадзора по безопасности АСУ ТП \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Ответь: \n\n"
            f"- Как работает датчик? (модель, протокол связи — Zigbee, LoRaWAN, MQTT) \n"
            f"- Что делать при срабатывании? (алгоритм: оповещение → локализация → вызов бригады) \n"
            f"- Куда направляется сигнал? (в диспетчерский центр, в ГИС ЖКХ, в мобильное приложение УК) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class MeetingAgent(RAGAgent):
    def __init__(self):
        super().__init__("Общие собрания", [
            "собрание", "осс", "голосование", "решение", "протокол", "кворум","акт приёмки", 
            "подписать акт", "приёмка работ", "совет дома", "председатель совета", "сдача объекта", 
            "ввод в эксплуатацию", "организация подписания","собрание", "протокол", "голосование", "кворум", 
            "повестка", "инициатор собрания", "как провести собрание", "ТСЖ", "недействительное собрание", "нарушение процедуры", 
            "оспаривание решения", "жалоба на решение", "повторное собрание", "инициатор собрания", "уведомление собственников", 
            "форма уведомления", "электронное голосование", "голосование через ГИС ЖКХ"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по ОСС. Ответь по ЖК РФ и сопутствующим актам: \n\n"
            f"- Основной документ: Жилищный кодекс РФ, глава 6 \n"
            f"- ПП РФ №416 — правила управления МКД \n"
            f"- ФЗ №217-ФЗ — особенности ОСС в СНТ (если применимо) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Добавь инструкцию: чтобы инициировать собрание, нужно уведомить всех собственников не менее чем за 10 дней \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующую проверку: \n\n"
            f"1. При упоминании любого решения общего собрания, ТЫ ОБЯЗАН уточнить: \n\n"
            f"   - 'Решение считается принятым, если за него проголосовало более 50% от общего числа голосов собственников в МКД (ст. 46 ЖК РФ).' \n"
            f"   - 'Если кворум не был достигнут, решение не имеет юридической силы и может быть оспорено в суде в течение 6 месяцев (ст. 46 ЖК РФ).' \n\n"
            f"Ответь: \n\n"
            f"- Как провести собрание онлайн? (ст. 47.1 ЖК РФ — через ГИС ЖКХ или иные платформы) \n"
            f"- Минимальный кворум? (ст. 48 ЖК РФ — более 50% от общего числа голосов) \n"
            f"- Срок оспаривания решения? (6 месяцев со дня, когда узнал — ст. 46 ЖК РФ) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class CapitalRepairAgent(RAGAgent):
    def __init__(self):
        super().__init__("Капитальный ремонт", [
            "капремонт", "капитальный ремонт", "откапиталить", "спецсчет", "специальный счет", "фонд",
            "программа капремонта", "программа капитального ремонта", "электрика", "электропроводка",
            "электроснабжение", "крыша", "лифт", "фасад", "труба", "инженерные сети", "подвал",
            "пожарная сигнализация", "вентиляция", "отопление", "холодное водоснабжение",
            "горячее водоснабжение", "вывод из эксплуатации", "ввод в эксплуатацию", "подрядчик капремонта",
            "надпись на фасаде", "вандализм", "граффити", "восстановить фасад", "восстановить стену",
            "очистить фасад", "ремонт фасада", "внешний вид дома", "реклама на фасаде", "заказчик работ", 
            "старший по дому", "специальный счёт",
            "капремонт", "спецсчёт", "региональный оператор", "взносы", "поменяли сроки", "платить за капремонт", "фонд капремонта",
            "смета капремонта", "отчет о расходовании средств", "выбор подрядчика", "приемка работ капремонта", "акт приемки капремонта",
            "перенос сроков капремонта", "изменение перечня работ", "фонд капремонта", "спецсчет капремонта", "региональный оператор капремонта"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по капремонту. Ответь по: \n\n"
            f"- ЖК РФ, глава 9 (ст. 166-180) \n"
            f"- ФЗ №271-ФЗ «О капитальном ремонте…» \n"
            f"- Региональная программа капремонта (официальный сайт региона) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Разъясни, что собственники могут открыть спецсчёт и контролировать расходы на капремонт \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО начни ответ с уточнения: \n\n"
            f"Важно различать текущий ремонт и капитальный ремонт. \n\n"
            f"- Текущий ремонт — это работы по предупреждению преждевременного износа и поддержанию работоспособности (ПП РФ №491, п. 2.3.1). Его проводит и оплачивает УК за счет платы за содержание жилья. \n"
            f"- Капитальный ремонт — это замена или восстановление строительных конструкций и инженерных сетей (ФЗ №271-ФЗ, ст. 2). Его оплачивают собственники за счет взносов на капремонт.' \n\n"
            f"Ответь: \n\n"
            f"- Когда запланирован ремонт? (год, этап — согласно региональной программе) \n"
            f"- Какие работы включены? (перечень из программы: крыша, фасад, лифт, инженерные сети) \n"
            f"- Можно ли перенести срок? (да, по решению ОСС — ст. 168 ЖК РФ, с согласования с Фондом капремонта) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class EmergencyAgent(RAGAgent):
    def __init__(self):
        super().__init__("Аварии и инциденты", [
            "авария", "отключили", "прорыв", "затопило", "нет воды", "нет света",
            "опасность", "угроза", "камень", "падение", "срочно", "ЧП", "безопасность",
            "шов", "стена", "фасад", "обрушение", "трагедия", "уберите", "вызов",
            "грозит", "аварийный", "немедленно", "риск", "травма", "смерть",
            "горячая вода", "холодная вода", "отопление", "теплотрасса", "ЦТП", "бойлер",
            "канализация", "стоки", "слив", "запах канализации", "откачка", "подвал затоплен", "течь канализации",
            "радиатор", "батарея", "не греет", "холодно в квартире", "отсутствие тепла", "требую замены",
            "перебои", "постоянные перебои", "без воды", "нет горячей воды", "полотенцесушитель холодный", 
            "ежемесячно оплачиваем", "по факту без воды", "яма", "раскопали", "магистральная труба", "параметры", 
            "температура воды", "нормативные параметры", "телефонограмма", "затопили соседи", "залило сверху", "протечка от соседей", 
            "акт о заливе", "возмещение ущерба", "комиссар","затопило", "залили соседи", "прорвало трубу", "сломался стояк", "пожар", 
            "аварийка", "вызвать аварийку", "куда звонить", "аварийная служба","возместить ущерб", "требую компенсации", "подать в суд за залив", 
            "оценка ущерба", "независимая экспертиза", "испортилась мебель", "отошла обои", "плесень после залива", "короткое замыкание",
            "внутридомовая сеть", "ввод в квартиру", "магистральный стояк"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — диспетчер аварийной службы. Ответь оперативно и структурированно по: \n\n"
            f"- ПП РФ №416 (обязательство АДС работать 24/7) \n"
            f"- ПП РФ №354 (определение аварии, сроки устранения) \n"
            f"- Правила №170 (сроки устранения дефектов) \n"
            f"- ФЗ №68-ФЗ — при угрозе ЧС \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора.\n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера.\n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика».\n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует» \n\n."
            f"Дай алгоритм: при затоплении → вызвать аварийную службу, составить акт, сфотографировать повреждения, при необходимости обращаться в суд о возмещении \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Определи и ЯВНО укажи в первом предложении ответа: 'Данная ситуация является [аварией / нарушением качества коммунальной услуги]'. \n\n"
            f"   - Критерии аварии: внезапное, непредвиденное событие, создающее угрозу жизни, здоровью или имуществу (прорыв трубы, обрушение, пожар). \n"
            f"   - Критерии нарушения качества: систематическое или разовое несоответствие услуги установленным нормативам (низкая температура, слабый напор). \n"
            f"   - Ссылка на определение: ПП РФ №354, п. 2, п. 98. \n\n"
            f"2. Если проблема в зоне РСО: \n\n"
            f"   - Предоставь контактные данные РСО (телефон, сайт, если есть в контексте). \n"
            f"   - Поясни, что УК направила им запрос (телефонограмму) и ждёт ответа. \n"
            f"   - Укажи ориентировочный срок восстановления, если он известен из контекста. \n\n"
            f"3. Если проблема в зоне УК: \n\n"
            f"   - Укажи срок восстановления (например: засор — 2 часа, отопление — 1 сутки — Правила №170). \n"
            f"   - Укажи телефон АДС УК для срочной связи: 347-00-01. \n\n"
            f"   - Если это **затопление от соседей**: \n\n"
            f"       *   Укажи: 'Немедленно звоните в АДС УК: 347-00-01. Не предпринимайте самостоятельных действий.' \n"
            f"       *   Объясни: 'Аварийный комиссар составит акт о заливе, который является основным документом для возмещения ущерба.' \n"
            f"       *   Добавь: 'Акт подписывается комиссией в присутствии пострадавшего и виновника (если он доступен).' \n"
            f"4. Не используй общие фразы вроде 'мы стараемся'. Давай конкретику. \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class ContractorAgent(RAGAgent):
    def __init__(self):
        super().__init__("Подрядчики и мастера", [
            "подрядчик", "мастер", "вызов", "рейтинг", "договор", "услуга",
            "уберите", "устраните", "проблема", "не решена", "обращались", "телефон",
            "фото", "вложении", "подъезд", "дом", "адрес", "стена", "шов", "фасад",
            "ремонт", "срочный", "вызовите", "отправьте", "бригаду",
            "когда", "срок", "план", "график", "дератизация", "кошение", "мытье окон", "лавочки", "урны",
            "горячая вода", "нет горячей воды", "отключение", "температура", "ребенок", "пожилой", "соцзащита",
            "надпись на фасаде", "вандализм", "граффити", "восстановить фасад", "восстановить стену",
            "очистить фасад", "реклама на фасаде","направить сантехника", "вызвать сантехника", "засор", "бурчит", 
            "гадость", "труба забита", "устранить засор", "сэс", "стройкерамика", "измельчены спецтехникой", "вывоз шин", 
            "покрышки", "акт приемки", "не подписан", "замечания",
            "кондиционер", "спутниковая антенна", "видеокамера", "согласование установки", "фасад дома", 
            "общедомовое имущество", "самовольная установка",
            "подрядчик", "ремонт фасада", "работы во дворе", "график работ", "замена труб", "вывоз мусора", "договор с подрядчиком",
            "некачественный ремонт", "халатность мастера", "не устранили проблему", "переделайте работу", "жалоба на подрядчика",
            "акт скрытых работ", "приемка-передача", "гарантийный срок", "претензия подрядчику", "договор подряда",
            "замена радиатора", "прочистка канализации", "ремонт кровли", "замена электропроводки"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        print(f"Результат: {web_results}")
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — координатор подрядчиков. Ответь по: \n\n"
            f"- ГК РФ, глава 37 — договор подряда \n"
            f"- ПП РФ №416 — сроки реагирования АДС \n"
            f"- ПП РФ №491 — перечень работ по содержанию общего имущества \n"
            f"- ФЗ №44-ФЗ — если работы по госзаказу \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Укажи, что жители вправе запросить смету и договор на работы, а подрядчик обязан их предоставить по требованию УК \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Определи тип запроса: \n\n"
            f"   - Если это **вопрос о документах** (договор, смета, акт скрытых работ, претензия): \n\n"
            f"       *   Укажи: 'Жители вправе запросить копию договора подряда и сметы на работы у управляющей компании (ПП РФ №491, п. 10).' \n"
            f"       *   Объясни: 'Акт скрытых работ подписывается до начала отделочных работ и является основанием для оплаты подрядчику.' \n"
            f"       *   Добавь: 'Претензию к качеству работ следует подавать в письменной форме в УК, которая обязана передать ее подрядчику и контролировать срок исправления (ПП РФ №416, п. 32).' \n\n"
            f"   - Если это **срочный вызов мастера** (протечка, засор, поломка): \n\n"
            f"       *   Укажи, как вызвать: через приложение УК, колл-центр, сайт ГИС ЖКХ. \n"
            f"       *   Укажи срок выполнения: срочные — до 24 ч, несрочные — до 72 ч (ПП РФ №416). \n\n"
            f"   - Если это **вопрос о плановой/запланированной работе** (кошение, дератизация, покраска, установка лавочек): \n\n"
            f"       *   Укажи статус: 'Запланировано', 'В работе', 'Выполнено'. \n"
            f"       *   Укажи конкретный срок или период выполнения, если он есть в контексте (например, 'до 19.09.2025', 'в сентябре 2025 года'). \n"
            f"       *   Если срок неизвестен, ответь: 'Ваша заявка/вопрос находится на рассмотрении. Специалист свяжется с вами для уточнения сроков.' \n\n"
            f"   - Если это **вопрос о том, почему УК не связывается** (как в данном случае): \n\n"
            f"       *   Начни с: 'Согласно ПП РФ №416, управляющая организация обязана отреагировать на заявку в течение 3 рабочих дней.' \n"
            f"       *   Дай инструкцию: 'Если прошло более 3 рабочих дней, позвоните в АДС по номеру 347-00-01 и назовите номер вашей заявки. Согласно внутреннему регламенту УК, информация о статусе заявки должна быть предоставлена в течение 1 рабочего дня после запроса.' \n"
            f"       *   Предложи альтернативу: 'Вы также можете отследить статус заявки в личном кабинете на сайте УК или в мобильном приложении.' \n\n"
            f"   - Если это **вопрос об установке оборудования на общедомовом имуществе** (кондиционер, спутниковая антенна, видеокамера): \n\n"
            f"       *   Укажи: 'Требуется согласование с УК, так как фасад/крыша/подвал являются общедомовым имуществом (ст. 36 ЖК РФ).' \n"
            f"       *   Дай инструкцию: 'Подайте письменное заявление в УК с указанием модели оборудования и предполагаемого места установки.' \n"
            f"       *   Предупреди: 'Самовольная установка может повлечь требование о демонтаже за счет собственника.' \n\n"
            f"2. Всегда указывай, где пользователь может указать адрес и прикрепить фото (например: в мобильном приложении УК → раздел «Заявки»). \n\n"
            f"3. Если работа требует решения общего собрания собственников (ОСС), ЯВНО укажи это и дай контакты специалиста в УК для подготовки документов. \n\n"
            f"4. Определи, требует ли запрашиваемая работа решения общего собрания собственников (ОСС). \n\n"
            f"   - Если работа связана с изменением общедомового имущества, его внешнего вида или не входит в стандартный перечень услуг по содержанию (ПП РФ №491), то решение ОСС ОБЯЗАТЕЛЬНО. \n"
            f"   - Примеры: установка видеокамер, кондиционеров, замена окон в подъезде, установка детской площадки. \n"
            f"   - Если решение ОСС требуется, но отсутствует, укажи: 'Выполнение данных работ невозможно без решения общего собрания собственников, так как они затрагивают общедомовое имущество (ст. 36 ЖК РФ). Для инициации собрания обратитесь в УК.' \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class HistoryAgent(RAGAgent):
    def __init__(self):
        super().__init__("История заявок", ["когда", "было", "прошлый", "история", "ранее", "делали", "ремонтировали"])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — архивариус ЖКХ. Ответь фактологически по: \n\n"
            f"- Внутреннему регламенту УК по ведению CRM \n"
            f"- ФЗ №152-ФЗ — сроки хранения персональных данных (не менее 3 лет)\n"
            f"- ПП РФ №354 — форма актов об устранении нарушений (п. 106) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Ответь: \n\n"
            f"- Дата последней заявки? (из CRM — формат ДД.ММ.ГГГГ) \n"
            f"- Кто выполнял? (ФИО мастера, название подрядной организации) \n"
            f"- Был ли акт? (да/нет, номер акта, дата подписания) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class FallbackAgent(RAGAgent):
    def __init__(self):
        # 🆕 Добавляем ключевые слова для базовых, общих вопросов по ЖКХ
        super().__init__("Fallback", [
            "кто работает", "структура жкх", "органы жкх", "управляющая компания", "тсж", "тсн",
            "ресурсоснабжающая организация", "рсо", "госжилинспекция", "гжи", "фонд капремонта",
            "муниципалитет", "администрация", "роспотребнадзор", "мчс", "прокуратура",
            "что такое", "объясни", "расскажи про", "основы жкх", "кто отвечает за", "кто занимается",
            "кто такой", "чем занимается", "функции", "полномочия", "жкх расшифровка", "сфера жкх",
            "кто в жкх", "кто работает в сфере", "организации жкх", "жилищная инспекция", "государственная жилищная инспекция", 
            "фонд содействия реформированию жкх", "министерство строительства", "тарифное регулирование", 
            "региональная служба по тарифам", "санитарно-эпидемиологическая станция", "пожарный надзор"
        ])
        # Триггеры для глупых/провокационных вопросов остаются
        self.trigger_phrases = [
            "дурак", "тупой", "идиот", "чмо", "лох", "придурок", "ненавижу", "не работает",
            "что ты умеешь", "кто ты", "ты кто", "что ты можешь", "для чего ты",
            "зачем ты", "как тебя зовут", "сколько тебе лет", "ты живой", "ты человек",
            "почему ты", "тест", "проверка", "hello", "привет", "здравствуй",
            "эй", "ой", "ага", "ок", "ладно", "понятно", "спасибо", "пожалуйста"
        ]
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def matches(self, query: str) -> bool:
        q = query.lower()
        # 🆕 Основная логика: если запрос содержит любое ключевое слово ИЛИ триггер — ловим
        if any(kw in q for kw in self.keywords):
            return True
        if any(phrase in q for phrase in self.trigger_phrases):
            return True
        # 🆕 Также ловим очень короткие сообщения (1-2 слова), если они не попали под другие агенты
        if len(q.split()) <= 2:
            return True
        return False

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        # Не используем стандартный промпт — ответ генерируется вручную или через LLM в generate_fallback_response
        return ""

    def generate_fallback_response(self, query: str) -> str:
        q = query.lower()

        # 🚫 Грубость / оскорбления — шаблонный ответ
        if any(word in q for word in ["дурак", "тупой", "идиот", "чмо", "лох", "придурок"]):
            return (
                "Я — виртуальный ассистент по вопросам ЖКХ. Меня можно критиковать, но лучше — задать конкретный вопрос. "
                "Например: *«Как передать показания счётчика?»* или *«Куда пожаловаться на протечку?»*. Я помогу!"
            )

        # ❓ Вопросы "что ты умеешь?" — шаблонный ответ
        if any(phrase in q for phrase in ["что ты умеешь", "кто ты", "ты кто", "что ты можешь", "для чего ты", "зачем ты"]):
            return (
                "Я — RAG-ассистент для сферы ЖКХ. Могу помочь вам:\n"
                "🔹 Рассчитать плату за ЖКУ\n"
                "🔹 Оспорить начисления\n"
                "🔹 Вызвать мастера или сообщить об аварии\n"
                "🔹 Узнать тарифы, нормативы, законы\n"
                "🔹 Отправить показания счётчиков\n"
                "🔹 Получить контакты аварийной службы\n\n"
                "Просто опишите проблему — я найду точный ответ на основе документов и практики."
            )

        # 👋 Приветствия / тесты — шаблонный ответ
        if any(phrase in q for phrase in ["привет", "здравствуй", "hello", "эй", "тест", "проверка"]):
            return (
                "Здравствуйте! Я — ваш ассистент по вопросам ЖКХ. "
                "Готов помочь с расчётами, авариями, законами, заявками. Просто опишите ситуацию — и я подскажу, что делать."
            )

        # 🧩 Короткие/бессмысленные сообщения — шаблонный ответ
        if len(q.split()) <= 2:
            return (
                "Пожалуйста, опишите ваш вопрос подробнее. Например: "
                "*«У меня не работает отопление»* или *«Неправильно начислили плату за воду»*. "
                "Чем точнее вопрос — тем лучше я смогу помочь!"
            )

        # 🎯 ВАЖНОЕ ИЗМЕНЕНИЕ: Для осмысленных, но нераспознанных вопросов — используем LLM!
        # Формируем промпт для генерации ответа
        web_results = self._perform_web_search(query)
        llm_prompt = (
            f"Ты — эксперт по жилищно-коммунальному хозяйству (ЖКХ). Ответь на вопрос пользователя кратко, информативно и вежливо. \n"
            f"Результаты поиска:{web_results}"
            f"Вопрос: «{query}» \n"
            f"Если вопрос касается структуры ЖКХ, обязанностей УК, ТСЖ, РСО, прав жильцов — дай развернутый ответ с пояснениями. \n"
            f"**ЖЕСТКОЕ ОГРАНИЧЕНИЕ: НЕ ИЗОБРЕТАЙ факты. Если ты не уверен в точном ответе, скажи: 'Этот вопрос требует уточнения. Обратитесь в вашу управляющую компанию или на портал ГИС ЖКХ.'** \n"
            f"Не извиняйся и не говори, что не знаешь. Постарайся быть максимально полезным. Ответ должен быть на русском языке. \n"
            f"Ассистент:[SEP]"
        )

        try:
            # --- ИСПРАВЛЕНИЕ: Используем НЕ rag_system.ask(), а ПРЯМОЙ вызов модели ---
            # Генерируем ответ через LLM БЕЗ повторного запуска всей RAG-логики
            inputs = tokenizer(llm_prompt, return_tensors="pt", truncation=True, max_length=1024).to(device)
            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=300,
                    temperature=0.3,
                    do_sample=True,
                    pad_token_id=tokenizer.eos_token_id
                )
            raw_answer = tokenizer.decode(outputs[0], skip_special_tokens=False)
            start_marker = "Ассистент:[SEP]"
            start = raw_answer.find(start_marker)
            if start != -1:
                answer = raw_answer[start + len(start_marker):].strip()
            else:
                answer = raw_answer.strip()

            # Очищаем ответ от лишних маркеров
            for stop in ["</s>", "Пользователь:", "Ассистент:", "\n\n"]:
                if stop in answer:
                    answer = answer.split(stop)[0].strip()

            # Если ответ слишком короткий или бессмысленный, возвращаем общий шаблон
            if len(answer.split()) < 5 or any(phrase in answer.lower() for phrase in ["не знаю", "не могу", "извините", "не понимаю"]):
                raise ValueError("Сгенерированный ответ слишком короткий или неинформативный")

            return answer

        except Exception as e:
            # Если LLM сломался или ответ неудовлетворительный — возвращаем запасной шаблон
            print(f"Ошибка генерации LLM в FallbackAgent: {e}")
            return (
                "Извините, я не совсем понял ваш запрос. \n\n"
                "Моя специализация — вопросы жилищно-коммунального хозяйства: расчёты, аварийные ситуации, нормативные акты, подача заявок, приборы учёта. \n\n"
                "Пожалуйста, переформулируйте вопрос, и я незамедлительно помогу!"
            )

class QualityControlAgent(RAGAgent):
    def __init__(self):
        super().__init__("Контроль качества услуг", [
            "качество", "холодно", "слабый напор", "не убирают", "жалоба", "претензия",
            "акт", "компенсация", "перерасчёт", "некачественно", "температура в квартире",
            "давление воды", "грязно", "воняет", "шум", "сквозняк", "влажность",
            "уборка", "дорога", "тротуар", "придомовая территория", "пыль", "грязь", "задыхаемся", "санитарное состояние", "мытье окон",
            "тараканы", "таракан", "дезинфекция", "дератизация", "обработка", "протравить", "вредители", "насекомые",
            "оповещение", "уведомление", "объявление", "информирование", "не предупредили", "не сообщили", "плановое отключение", 
            "отключение воды","из месяца в месяц", "систематически", "регулярно не моют", "фото прикладываю", "доказательства", 
            "жалобы игнорируются", "акт выполненных работ", "разъяснительная беседа","уборка подъезда", "грязь", "не моют полы", 
            "снег во дворе", "не убирают мусор", "жалоба на УК", "плохо убирают", "акт проверки","систематические нарушения", 
            "регулярные жалобы", "игнорирование актов", "жалоба в прокуратуру", "жалоба в Роспотребнадзор", "проверка ГЖИ", 
            "проверка Роспотребнадзора", "акт проверки", "предписание", "штраф для УК", "понижение тарифа"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по контролю качества коммунальных услуг. Ответь по: \n\n"
            f"- ПП РФ №354, Приложение №1 — условия признания услуги ненадлежащего качества\n"
            f"- ПП РФ №354, п. 98-106 — порядок составления актов и перерасчёта \n"
            f"- ФЗ №59-ФЗ — подача жалобы в УК/ГЖИ \n"
            f"- СанПиН 2.1.2.2645-10 — нормы микроклимата \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера.\n\n"
            f"Разъясни, что жители могут составить акт ненадлежащего качества услуги и направить его в УК. Если реакции нет — жалоба в ГЖИ \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО приведи точную формулу для расчета перерасчета, если вопрос касуется снижения платы: \n\n"
            f"- Для отопления: 'Размер платы снижается на 0.15% за каждый час превышения допустимой продолжительности предоставления коммунальной услуги ненадлежащего качества (ПП РФ №354, п. 98).' \n"
            f"- Для ГВС/ХВС: 'Размер платы снижается пропорционально объему непредоставленной услуги, определенному по показаниям приборов учета или по нормативу (ПП РФ №354, п. 99).' \n\n"
            f"Ответь: \n\n"
            f"- Как зафиксировать нарушение? (акт с участием УК, фото, термометр, манометр) \n"
            f"- Как добиться перерасчёта? (подать заявление + акт → ПП РФ №354, п. 106) \n"
            f"- Куда жаловаться, если УК игнорирует? (ГЖИ, Роспотребнадзор, через ГИС ЖКХ) \n"
            f"- Положена ли компенсация? (да, если услуга признана некачественной — ПП РФ №354, п. 98) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class PaymentDocumentsAgent(RAGAgent):
    def __init__(self):
        super().__init__("Платёжные документы", [
            "квитанция", "платёжка", "провodka", "чек", "ЕПД", "расшифровка платежа",
            "что значит эта строка", "почему такая сумма", "ошибка в квитанции",
            "единолицевой счет", "ИПД", "ЖКУ", "назначение платежа", "реквизиты",
            "квитанция", "епд", "платёжный документ", "расшифровка", "строки в квитанции", "как понять платёжку", "где долг в квитанции"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по платёжным документам ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Основной документ: ПП РФ №354 (форма платёжного документа) \n"
            f"- ГОСТ Р 56042-2014 — формат штрих-кодов и реквизитов \n"
            f"- ПП РФ №731 — стандарт раскрытия информации в платёжках \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Дай пояснение по строкам ЕПД: жилищные услуги, коммунальные услуги, ОДН \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Что означает каждая строка в квитанции? (ПП РФ №354, Приложение №1) \n"
            f"- Как проверить корректность начислений? (сверка объёмов, тарифов, нормативов) \n"
            f"- Как расшифровать реквизиты: ЕЛС, ИПД, ЖКУ? (ГОСТ Р 56042-2014) \n"
            f"- Что делать при ошибке в квитанции? (заявление в УК, перерасчёт — ПП РФ №354, п. 106) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class BillingAuditAgent(RAGAgent):
    def __init__(self):
        super().__init__("Аудит начислений", [
            "аудит квитанции", "проверка начислений", "почему резко выросла плата",
            "непонятные услуги", "завышенный тариф", "повышающий коэффициент",
            "проверка УК", "аномалия в расчёте", "скрытые услуги", "необоснованное начисление",
            "неверные начисления", "ошибка в квитанции", "проверка начислений", "аудит ЖКХ", "сравнить тарифы", "почему завышено"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — аудитор начислений ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- ПП РФ №354 — правила расчёта платы (гл. V-VII) \n"
            f"- Приказ Минстроя №43/пр — методика расчёта ОДН \n"
            f"- Региональные тарифы (официальный сайт РСТ региона) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Объясни, что можно запросить детализацию расчёта и сравнить с нормативами. Если УК отказывает — жалоба в ГЖИ или суд \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Применён ли повышающий коэффициент правильно? (ПП РФ №354, п. 42) \n"
            f"- Соответствует ли тариф официальному? (ссылка на сайт РСТ региона) \n"
            f"- Корректен ли расчёт ОДН? (Приказ Минстроя №43/пр) \n"
            f"- Как оспорить завышенное начисление? (акт + заявление — ПП РФ №354, п. 106) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class SubsidyAndBenefitsAgent(RAGAgent):
    def __init__(self):
        super().__init__("Льготы и субсидии", [
            "льгота", "субсидия", "компенсация", "скидка", "многодетный", "инвалид",
            "ветеран", "доход ниже прожиточного", "как оформить субсидию", "пособие на ЖКХ",
            "региональная льгота", "федеральная льгота", "доход на члена семьи",
            "льготы", "субсидия", "ветераны", "инвалиды", "многодетные", "компенсация", "как оформить льготу", "куда подавать документы",
            "отказ в льготе", "отказ в субсидии", "приостановление субсидии", "перерасчет субсидии", "возврат излишне выплаченной субсидии",
            "льгота по оплате", "льгота по нормативу", "компенсация части платы", "региональная доплата", "федеральная льгота"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по льготам и субсидиям ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n"
            f"- ЖК РФ, ст. 159 — условия предоставления субсидий \n"
            f"- ПП РФ №761 — правила предоставления субсидий \n"
            f"- ФЗ №178-ФЗ — государственная социальная помощь \n"
            f"- Региональные законы — пороги доходов и категории льготников \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"Укажи, что субсидии оформляются в МФЦ или через ГИС ЖКХ, а льготы предоставляются по федеральным и региональным законам \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Положена ли субсидия? (ЖК РФ, ст. 159 — если расходы > регионального стандарта) \n"
            f"- Какие документы нужны? (ПП РФ №761, Приложение №1) \n"
            f"- Как подать заявление? (через Госуслуги, МФЦ, органы соцзащиты) \n"
            f"- Срок действия и переоформления? (6–12 месяцев — ПП РФ №761, п. 27) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class LegalClaimsAgent(RAGAgent):
    def __init__(self):
        super().__init__("Юридические претензии", [
            "претензия", "иск", "суд", "жалоба в прокуратуру", "взыскание",
            "неустойка", "моральный вред", "образец заявления", "срок исковой давности",
            "досудебное урегулирование", "жалоба в ГЖИ", "обращение в Роспотребнадзор",
            "образец претензии", "образец иска", "госпошлина", "подсудность", "доказательства", "свидетельские показания",
            "нотариальные документы", "экспертиза", "судебный приказ", "исковое заявление", "ходатайство"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — юрист по жилищным спорам. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- ГК РФ, ст. 309, 310 — обязательства и ответственность \n"
            f"- ЖК РФ, ст. 154-157 — порядок оплаты и ответственность за неисполнение \n"
            f"- ПП РФ №354, п. 98-106 — перерасчёт при нарушении качества \n"
            f"- ФЗ №2300-1 — права потребителей \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера.\n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Как составить досудебную претензию? (ФЗ №2300-1, ст. 17) \n"
            f"- Как рассчитать неустойку? (1/300 ставки ЦБ за день просрочки — ст. 155 ЖК РФ) \n"
            f"- Срок исковой давности? (3 года — ст. 196 ГК РФ) \n"
            f"- Куда подавать иск? (мировой суд — до 500 тыс. руб., районный — свыше)\n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class DebtManagementAgent(RAGAgent):
    def __init__(self):
        super().__init__("Управление долгами", [
            "коллектор", "судебный пристав", "реструктуризация долга", "рассрочка",
            "исполнительное производство", "арест счета", "запрет выезда", "письмо от коллектора",
            "как списать долг", "истечение срока давности", "банкротство физлица"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает результаты в виде текста.
        """
        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query + " ЖКХ", max_results=max_results):
                    # Форматируем результат: Заголовок + Краткое описание + Ссылка
                    result_text = f"**{r['title']}** {r['body']} Источник: {r['href']} "
                    results.append(result_text)
            if results:
                return "".join(results)
            else:
                return "По вашему запросу ничего не найдено в интернете."
        except Exception as e:
            print(f"Ошибка веб-поиска: {e}")
            return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — юрист по долгам ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- **Прямо укажи формулу расчета пени:** 'Размер пени = Сумма долга × (Ключевая ставка ЦБ РФ / 300) × Количество дней просрочки' (ст. 155 ЖК РФ). Не пиши общих фраз, вставь эту формулу дословно. \n"
            f"- ГК РФ, ст. 196 — срок исковой давности (3 года) \n"
            f"- ФЗ №229-ФЗ — исполнительное производство \n"
            f"- ПП РФ №354, п. 69 — рассрочка платежа по соглашению \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующую проверку: \n\n"
            f"1. Перед тем как давать любые рекомендации по долгу, ТЫ ОБЯЗАН проверить срок исковой давности. \n\n"
            f"   - Напомни пользователю: 'Срок исковой давности по долгам ЖКХ составляет 3 года (ст. 196 ГК РФ). Если с момента последнего платежа или признания долга прошло более 3 лет, долг может быть оспорен в суде.' \n"
            f"   - Уточни: 'Срок давности применяется только по заявлению должника. Если вы не заявили о пропуске срока в суде, долг подлежит взысканию.' \n\n"
            f"2. Если вопрос касается **ограничения ставки 9,5%** при расчете пеней: \n\n"
            f"   - Начни с: 'Ограничение ставки для расчета пеней установлено Федеральным законом от 08.03.2015 № 44-ФЗ, который внес изменения в ст. 155 Жилищного кодекса РФ.' \n"
            f"   - Поясни: 'Согласно этому закону, если ключевая ставка Центрального банка РФ превышает 9,5% годовых, то для расчета пеней применяется значение 9,5%.' \n\n"
            f"Структурируй ответ: \n\n"
            f"- Как оформить рассрочку? (ПП РФ №354, п. 69 — по соглашению с УК) \n"
            f"- Что делать при звонках коллекторов? (ФЗ №230 — ограничения на общение) \n"
            f"- Как проверить законность долга? (сверка с ГИС ЖКХ, срок давности — ст. 196 ГК РФ) \n"
            f"- Как списать долг? (по решению суда, банкротство, истечение срока давности) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class IoTIntegrationAgent(RAGAgent):
    def __init__(self):
        super().__init__("Интеграция с IoT", [
            "умный счётчик", "автоматическая передача", "данные с датчика",
            "аномальное потребление", "утечка воды", "энергомониторинг",
            "интеграция с приложением", "MQTT", "Zigbee", "LoRaWAN", "датчик температуры", 
            "датчик протечки", "датчик задымления", "умный термостат", "умный счетчик воды", "умный счетчик тепла",
            "интеграция с умным домом", "уведомления в телеграм", "уведомления в whatsapp", "API для интеграции", "вебхуки"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — инженер IoT в ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- ФЗ №152-ФЗ — обработка персональных данных \n"
            f"- ГОСТ Р 57580 — системы умного дома \n"
            f"- ПП РФ №354, п. 31 — порядок передачи показаний \n"
            f"- Технические регламенты Ростехнадзора по АСУ ТП \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Как настроить автоматическую передачу показаний? (ПП РФ №354, п. 31) \n"
            f"- Как интерпретировать данные с датчиков? (сравнение с нормативами СанПиН 2.1.2.2645-10) \n"
            f"- Что делать при аномалии? (автоматическая заявка в АДС с прикреплением фото/видео) \n"
            f"- Как интегрировать с ГИС ЖКХ или приложением УК? (API, форматы данных, протоколы MQTT/HTTP) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class WasteManagementAgent(RAGAgent):
    def __init__(self):
        super().__init__("Вывоз ТКО", [
            "тко", "мусор", "вывоз мусора", "тариф на вывоз", "региональный оператор",
            "контейнер", "мусорная площадка", "перерасчёт за мусор", "не вывозят мусор",
            "тариф тко", "объём тко", "норматив накопления", "раздельный сбор", "сортировка",
            "запах", "крысы", "мухи", "переполненный контейнер", "график вывоза", "расчет за тко",
            "не убрали", "свалка", "незаконная свалка", "перерасчет тко",
            "обращение по тко", "жалоба на мусор", "ответственность за контейнеры", "скопилось", 
            "скопление", "навалено", "навал", "свалка", "несанкционированная свалка", "пожароопасный", 
            "опасный", "автошины", "покрышки", "резина","крысы", "крыса", "мыши", "грызуны", "дератизация", 
            "дезинфекция", "санитарное состояние", "санэпидемстанция", "сэс",
            # --- Ключевые слова для триггера класса опасности ---
            "класс опасности", "не относится к тко", "захоронение запрещено", "спецтехника", "фронтальный погрузчик",
            "батарейки", "лампочки", "энергосберегающие лампы", "ртутьсодержащие", "градусник", "термометр", "медицинские отходы", 
            "пункт приема", "воняет как на свалке", "крысы бегают", "мухи кишмя", "контейнеры переполнены", "автошины", "покрышки", 
            "резина","батарейки", "лампочки", "энергосберегающие лампы", "ртутьсодержащие", "градусник", "термометр", "медицинские отходы", 
            "пункт приема"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по обращению с твердыми коммунальными отходами (ТКО). Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n"
            f"- Основной документ: ФЗ №89-ФЗ «Об отходах производства и потребления» \n"
            f"- ПП РФ №354, Раздел VIII (ст. 148-156) — расчет платы за ТКО, порядок перерасчета \n"
            f"- ПП РФ №491 — содержание общего имущества, включая мусорные площадки \n"
            f"- Региональные нормативы накопления ТКО (утверждаются органами власти субъектов РФ) \n"
            f"- Тарифы регионального оператора (официальный сайт РО в вашем регионе) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"--- КРИТИЧЕСКАЯ ИНСТРУКЦИЯ --- \n\n"
            f"Если в вопросе пользователя или в контексте упоминается КОНКРЕТНЫЙ ВИД ОТХОДА (например: автошины, покрышки, батарейки, лампы, градусники, мебель, техника, строительный мусор), ТЫ ОБЯЗАН ВКЛЮЧИТЬ В НАЧАЛО ОТВЕТА СЛЕДУЮЩУЮ СТРУКТУРИРОВАННУЮ ИНФОРМАЦИЮ: \n\n"
            f"**Классификация отхода '{summary}':** \n\n"
            f"1.  **Класс опасности:** [Укажи класс опасности, если он известен из контекста, например, '4 класс опасности'. Если неизвестен, напиши 'Класс опасности не указан в контексте.']. \n"
            f"2.  **Относится ли к ТКО:** [Четко укажи 'Да' или 'Нет'. Если 'Нет', объясни почему, например, 'Не относится к ТКО согласно ФЗ №89-ФЗ.']. \n"
            f"3.  **Разрешено ли захоронение:** [Укажи 'Да' или 'Нет'. Если 'Нет', добавь 'Захоронение запрещено согласно ФЗ №89-ФЗ.']. \n"
            f"4.  **Кто отвечает за утилизацию:** [Укажи, кто несет ответственность, например, 'Собственник автотранспортного средства' или 'Гражданин-потребитель']. \n"
            f"5.  **Как утилизировать:** [Укажи, куда можно сдать, например, 'В специализированные пункты приема' или 'Обратиться к подрядчику для вывоза спецтехникой']. \n\n"
            f"--- КОНЕЦ КРИТИЧЕСКОЙ ИНСТРУКЦИИ --- \n\n"
            f"--- ДОПОЛНИТЕЛЬНАЯ КРИТИЧЕСКАЯ ИНСТРУКЦИЯ ---  \n\n"
            f"Если в вопросе или контексте речь идет о смешивании отходов (например, батарейки с бытовым мусором), ТЫ ОБЯЗАН НАЧАТЬ ОТВЕТ С: \n\n"
            f"**ЗАПРЕЩЕНО!** Смешивание отходов разных классов опасности строго запрещено Федеральным законом №89-ФЗ 'Об отходах производства и потребления'. \n\n"
            f"Это нарушение влечет административную ответственность по ст. 8.2 КоАП РФ. \n\n"
            f"Отходы должны быть разделены и утилизированы строго по их классу опасности. \n\n"
            f"После этого, структурируй основной ответ: \n\n"
            f"- Как рассчитывается плата за ТКО? (по количеству проживающих или по площади? ПП РФ №354, п. 148) \n"
            f"- Как добиться перерасчета, если услуга не оказана? (акт, заявление в УК/РО — ПП РФ №354, п. 154) \n"
            f"- Кто отвечает за состояние контейнерной площадки? (содержание — УК/ТСЖ по ПП РФ №491, вывоз — региональный оператор по ФЗ №89-ФЗ) \n"
            f"- Куда жаловаться на несвоевременный вывоз или антисанитарию? (на УК — в ГЖИ, на РО — в региональный орган, контролирующий ТКО, или Росприроднадзор) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class AccountManagementAgent(RAGAgent):
    def __init__(self):
        super().__init__("Управление лицевыми счетами", [
            "лицевой счет", "лицевой счёт", "объединить счета", "разделить счет", "переоформить счет",
            "открыть счет", "закрыть счет", "единый лицевой счет", "единый лицевой счёт", "едлс",
            "доверенность", "по доверенности", "нотариальная доверенность", "генеральная доверенность",
            "собственник", "не собственник", "правоустанавливающие документы", "выписка егрн",
            "договор купли-продажи", "дарственная", "наследство", "передача прав","паспортный стол","смена собственника", 
            "вступление в наследство", "дарение квартиры", "купля-продажа", "регистрация права",
            "выписка из ЕГРН", "технический паспорт", "кадастровый паспорт", "изменение состава семьи", "временная регистрация",
            "прописка", "регистрация", "регистрация по месту жительства", "документы для регистрации", "оформить прописку", 
            "где оформить регистрацию"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по управлению лицевыми счетами в ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Жилищный кодекс РФ, ст. 154 — структура платы за жилое помещение и коммунальные услуги \n"
            f"- ПП РФ №354 — правила предоставления коммунальных услуг (гл. III — плата за услуги) \n"
            f"- ФЗ №152-ФЗ «О персональных данных» — при передаче данных третьим лицам \n"
            f"- Гражданский кодекс РФ, гл. 10 — доверенность (ст. 185-189) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Можно ли объединить/разделить лицевой счет? (ЖК РФ, ст. 154 — лицевой счет открывается на помещение, а не на человека) \n"
            f"- Кто может подать заявление? (собственник или его представитель по нотариальной доверенности — ГК РФ, ст. 185) \n"
            f"- Какие документы нужны? (выписка ЕГРН, паспорт, нотариальная доверенность, заявление) \n"
            f"- Где получить справку для субсидии? (в расчетном отделе УК/ЖЭУ, срок изготовления — 3 рабочих дня, необходим паспорт — ФЗ №152-ФЗ) \n"
            f"- Куда обращаться? (в управляющую компанию или расчетно-кассовый центр — как в примере из контекста: adm@spas-dom.ru или паспортный стол) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class ContractAndMeetingAgent(RAGAgent):
    def __init__(self):
        super().__init__("Договоры и решения ОСС", [
            "договор", "контракт", "соглашение", "расторгнуть", "расторжение", "заключен", "подписан",
            "осс", "общее собрание", "протокол", "решение собрания", "решение осс", "голосование",
            "аренда", "реклама в лифте", "реклама в подъезде", "земельный участок", "ип", "ооо",
            "рекламная компания", "проверить договор", "статус договора", "юридическая сила",
            "списала деньги", "нецелевое использование", "собранные средства", "нарушение решения осс", "право распоряжаться", 
            "целевые средства", "собрание", "компенсация долгов","принимал работы", "приемка работ", "акт приемки", "ответственность подрядчика", "испортили имущество", 
            "восстановить дверь", "кто виноват", "некачественный ремонт"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — юрист, специализирующийся на договорах и решениях общих собраний собственников (ОСС) в сфере ЖКХ. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Жилищный кодекс РФ, глава 6 — Порядок управления многоквартирным домом, решения ОСС (ст. 44-48) \n"
            f"- Гражданский кодекс РФ, глава 29 — Обязательства из договоров (ст. 420-453) \n"
            f"- ПП РФ №416 — Правила управления МКД \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, приказы, письма Минстроя, разъяснения Ростехнадзора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Действительно ли решение ОСС? (ЖК РФ, ст. 46 — решение считается принятым, если за него проголосовало более 50% от общего числа голосов) \n"
            f"- Заключен ли договор на основании решения ОСС? (ГК РФ, ст. 432 — договор считается заключенным, если между сторонами достигнуто соглашение по всем существенным условиям) \n"
            f"- Расторгнут ли договор? (ГК РФ, ст. 450 — основания для расторжения: соглашение сторон или решение суда) "
            f"- Где можно получить копию договора или протокола? (в управляющей компании, в ГИС ЖКХ) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class RegionalMunicipalAgent(RAGAgent):
    def __init__(self):
        super().__init__("Региональные и муниципальные акты", [
            "региональный закон", "муниципальный акт", "закон субъекта", "постановление мэрии", "распоряжение губернатора",
            "тариф в [регион]", "норматив в [город]", "программа капремонта [регион]", "льготы в [субъект]",
            "местные правила", "муниципальные нормы", "акт местного самоуправления", "закон [область/край/республика]",
            "постановление [название города]", "тариф на отопление в москве", "норматив по тко в спб"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — эксперт по региональному и муниципальному законодательству в сфере ЖКХ. Ответь строго по документам: \n\n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: Закон субъекта РФ, Постановление Правительства субъекта РФ, Распоряжение, Решение Думы/Совета депутатов, Постановление мэрии. \n"
            f"- Формат: «Закон Челябинской области №XXX-ЗО, ст. Y» или «Постановление Правительства Москвы №XXX-ПП, п. Y» — строго в таком виде. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Уточни, что региональные акты не могут противоречить федеральному законодательству (ЖК РФ, ФЗ). \n\n"
            f"Структурируй ответ: \n\n"
            f"- Какой именно региональный/муниципальный акт регулирует данный вопрос? \n"
            f"- Каковы его основные положения? \n"
            f"- Где можно найти полный текст акта? (официальный сайт региона/города) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class CourtPracticeAgent(RAGAgent):
    def __init__(self):
        super().__init__("Судебная практика и разъяснения", [
            "судебная практика", "разъяснения вс рф", "постановление пленума", "определение вс", "решение суда",
            "позиция верховного суда", "как суды трактуют", "арбитражная практика", "обзор судебной практики",
            "определение вс рф", "постановление пленума вс рф", "обзор практики", "разъяснение минстроя", "письмо ростехнадзора"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — юрист, специализирующийся на судебной практике и разъяснениях высших судов в сфере ЖКХ. Ответь строго по документам: \n\n"
            f"- Основной источник: Определения и Постановления Верховного Суда РФ, Постановления Пленума ВС РФ, Обзоры судебной практики. \n"
            f"- Вторичные источники: Письма и разъяснения Минстроя РФ, Ростехнадзора, Роспотребнадзора. \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник. \n"
            f"- Формат для судебных решений: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая суть». \n"
            f"- Формат для разъяснений: «Письмо Минстроя РФ от ДД.ММ.ГГГГ №XXX». \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Нормативная база». Укажи, на какие статьи ЖК РФ, ФЗ или ПП РФ опирается судебная практика. \n\n"
            f"Объясни, что судебная практика является источником права и обязательна для нижестоящих судов. \n\n"
            f"Структурируй ответ: \n\n"
            f"- Какова позиция Верховного Суда по данному вопросу? \n"
            f"- На какие нормы закона она ссылается? \n"
            f"- Как это применить на практике? \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class LicensingControlAgent(RAGAgent):
    def __init__(self):
        super().__init__("Лицензирование и контроль за УК", [
            "лицензия ук", "гжи", "госжилинспекция", "проверка ук", "отзыв лицензии", "нарушение лицензии",
            "жалоба в гжи", "проверка госжилинспекции", "предписание гжи", "ответственность ук", "штраф для ук",
            "реестр лицензий", "проверить лицензию ук", "условия лицензии", "требования к ук", "отчетность ук в гжи"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по лицензированию и государственному контролю в сфере ЖКХ. Ответь строго по документам: \n\n"
            f"- Основной документ: ФЗ №99-ФЗ «О лицензировании отдельных видов деятельности» (Глава 4.1) \n"
            f"- ПП РФ №256 — Правила лицензирования деятельности по управлению МКД \n"
            f"- ПП РФ №416 — Правила управления МКД \n"
            f"- Кодекс РФ об административных правонарушениях (КоАП РФ) — штрафы \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, КоАП РФ. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Каковы основания для отзыва лицензии? (ФЗ №99-ФЗ, ст. 19.1) \n"
            f"- Как подать жалобу в ГЖИ? (ФЗ №59-ФЗ, через ГИС ЖКХ или письменно) \n"
            f"- Каковы сроки рассмотрения жалобы? (30 дней — ФЗ №59-ФЗ, ст. 12) \n"
            f"- Какие штрафы предусмотрены для УК? (КоАП РФ, ст. 7.23, 14.1.3) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class RSOInteractionAgent(RAGAgent):
    def __init__(self):
        super().__init__("Взаимодействие с РСО", [
            "рсо", "ресурсоснабжающая организация", "прямой договор с рсо", "акт сверки с рсо", "передача показаний рсо",
            "начисления рсо", "платеж рсо", "отключение рсо", "качество услуги рсо", "ответственность рсо", "граница балансовой принадлежности",
            "тепловая сеть", "водопроводная сеть", "канализационная сеть", "электросети", "газовые сети", "передача данных", "интеграция с рсо"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — эксперт по взаимодействию между УК/ТСЖ и Ресурсоснабжающими организациями (РСО). Ответь строго по документам: \n\n"
            f"- Основной документ: ПП РФ №354 — Правила предоставления коммунальных услуг \n"
            f"- ПП РФ №307 — Правила предоставления коммунальных услуг (для прямых договоров) \n"
            f"- ПП РФ №554 — Правила функционирования ресурсоснабжающих организаций \n"
            f"- Договоры энергоснабжения, водоснабжения и т.д. \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, условия договора. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде. \n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Определи зону ответственности: где проходит граница балансовой принадлежности? (ПП РФ №354, Приложение №1) \n"
            f"2. Если проблема в зоне РСО: укажи, что УК должна направить запрос в РСО и ждать их ответа. \n"
            f"3. Если проблема в зоне УК: укажи, что УК обязана решить ее самостоятельно. \n"
            f"4. Объясни порядок передачи показаний и проведения актов сверки. \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class SafetySecurityAgent(RAGAgent):
    def __init__(self):
        super().__init__("Безопасность и антитеррористическая защищенность", [
            "пожарная безопасность", "антитеррор", "антитеррористическая защищенность", "пожаротушение", "пожарная сигнализация",
            "система оповещения", "пожарный щит", "огнетушитель", "эвакуационный выход", "пожарная лестница", "пожарный кран",
            "пожарный гидрант", "пожарный надзор", "мчс", "проверка мчс", "пожарный сертификат", "пожарный аудит", "пожарный минимум"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
    
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — инженер по пожарной безопасности и антитеррористической защищенности в ЖКХ. Ответь строго по документам: \n\n"
            f"- Основной документ: ФЗ №123-ФЗ «Технический регламент о требованиях пожарной безопасности» \n"
            f"- ФЗ №390-ФЗ «О безопасности» \n"
            f"- Постановление Правительства РФ №1006 — Правила противопожарного режима \n"
            f"- СП 54.13330.2016 — Свод правил по пожарной безопасности для МКД \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, СП, ГОСТ. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Какие системы пожарной безопасности должны быть в МКД? (СП 54.13330.2016) \n"
            f"- Кто отвечает за их содержание и исправность? (УК/ТСЖ — ПП РФ №491) \n"
            f"- Как часто проводятся проверки? (ПП РФ №1006) \n"
            f"- Что делать при обнаружении нарушения? (жалоба в МЧС или ГЖИ) \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

class EnergyEfficiencyAgent(RAGAgent):
    def __init__(self):
        super().__init__("Энергосбережение и энергоэффективность", [
            "энергосбережение", "энергоэффективность", "фз 261", "энергетическое обследование", "энергоаудит",
            "общедомовой прибор учета", "одпу", "индивидуальный прибор учета", "ипу", "тепловизионное обследование",
            "утепление фасада", "замена окон", "модернизация систем", "энергосервисный контракт", "эск", "энергосберегающие технологии"
        ])
        
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."   
    
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по энергосбережению и повышению энергоэффективности в ЖКХ. Ответь строго по документам: \n\n"
            f"- Основной документ: ФЗ №261-ФЗ «Об энергосбережении и о повышении энергетической эффективности» \n"
            f"- ПП РФ №603 — Правила установления требований энергетической эффективности \n"
            f"- ПП РФ №354 — Правила предоставления коммунальных услуг (разделы по приборам учета) \n"
            f"- Приказ Минстроя №721/пр — Методические рекомендации по проведению энергетических обследований \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, Приказ. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде. \n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"Структурируй ответ: \n\n"
            f"- Обязан ли собственник установить прибор учета? (ФЗ №261-ФЗ, ст. 13) \n"
            f"- Кто должен проводить энергетическое обследование? (ФЗ №261-ФЗ, ст. 16) \n"
            f"- Какие меры по энергосбережению можно реализовать в МКД? \n"
            f"- Что такое энергосервисный контракт и как он работает? \n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class ReceiptProcessingAgent(RAGAgent):
    def __init__(self):
        super().__init__("Обработка чеков и платежных документов", [
            "чек", "скан чека", "фото чека", "qr-код", "фискальный чек", "фн", "фд", "фпд",
            "тег", "теги чека", "структура чека", "расшифровка тегов", "ошибка в чеке", "неверный чек",
            "чек не проходит", "чек не считывается", "автоматическая обработка", "интеграция с бухгалтерией",
            "xml чек", "json чек", "офд", "оператор фискальных данных", "фискальный накопитель", "фискальный признак",
            "признак расчёта", "кассовый чек", "бланк строгой отчетности", "бсо", "онлайн-касса", "54-фз",
            "фискальный документ", "фискальные данные", "тег 1008", "тег 1020", "тег 1054", "тег 1055", "тег 1081",
            "тег 1102", "тег 1162", "тег 1163", "тег 1187", "тег 1192", "тег 1203", "тег 1207", "тег 1227", "платежный агент",
            "поставщик"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по обработке фискальных чеков и платежных документов. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Основной документ: ФЗ №54-ФЗ «О применении контрольно-кассовой техники» \n"
            f"- Постановление Правительства РФ №745 — Правила регистрации ККТ \n"
            f"- Приказ ФНС России №ЕД-7-20/662@ — Форматы фискальных документов \n"
            f"- ГОСТ Р 59244-2020 — Форматы данных для обмена информацией с ОФД \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, Приказ ФНС, ГОСТ. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «Приказ ФНС №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Если вопрос касается **расшифровки тегов чека**: \n\n"
            f"   - Укажи: 'Теги — это структурированные данные в фискальном документе, определенные Приказом ФНС №ЕД-7-20/662@.' \n"
            f"   - Приведи таблицу с расшифровкой запрошенных тегов (например, тег 1054 — Признак предмета расчета, тег 1203 — Цена за единицу). \n"
            f"   - Объясни: 'Тег 1081 (Признак способа расчета) и тег 1054 (Признак предмета расчета) являются обязательными и критичными для валидности чека.' \n\n"
            f"2. Если вопрос касается **ошибки в чеке или его невалидности**: \n\n"
            f"   - Начни с: 'Согласно ФЗ №54-ФЗ, ст. 4.7, чек считается недействительным, если в нем отсутствуют обязательные реквизиты.' \n"
            f"   - Перечисли обязательные реквизиты: название организации, ИНН, адрес расчетов, признак расчета, сумма, ФН, ФД, ФПД. \n"
            f"   - Укажи: 'Если чек не проходит проверку на сайте ФНС или в ОФД, это означает, что он не был зарегистрирован в фискальной системе.' \n\n"
            f"3. Если вопрос касается **автоматической обработки чеков**: \n\n"
            f"   - Объясни: 'Для автоматической обработки чеков необходимо использовать их XML/JSON-версию, полученную от ОФД или через QR-код.' \n"
            f"   - Укажи: 'Интеграция с бухгалтерскими системами (1С, SAP) осуществляется через API ОФД или парсинг структурированных данных.' \n"
            f"   - Предупреди: 'Фотографии чеков требуют OCR-распознавания, что менее надежно, чем работа с фискальными тегами.' \n\n"
            f"4. Всегда указывай, где можно проверить подлинность чека: \n\n"
            f"   - 'Официальный сайт ФНС: https://check.ofd.ru' \n"
            f"   - 'Мобильное приложение вашего ОФД (например, Такском, Платформа ОФД)' \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )
        
class PassportRegistrationAgent(RAGAgent):
    def __init__(self):
        super().__init__("Паспортный учет и регистрация", [
            "прописка", "регистрация", "регистрация по месту жительства", "выписка", "выписаться",
            "прописаться", "оформить прописку", "документы для регистрации", "где оформить регистрацию",
            "паспортный стол", "мфц регистрация", "госуслуги прописка", "форма №6", "форма №7",
            "снятие с регистрационного учета", "постановка на регистрационный учет", "миграционный учет",
            "миграция", "миграционный пункт", "отдел по вопросам миграции", "омвд", "паспортный учет",
            "справка о регистрации", "подтверждение регистрации", "временная регистрация", "постоянная регистрация",
            "документы паспортисту", "что нужно для прописки", "как выписаться из квартиры", "как прописаться в квартиру"
        ])
    
    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Выполняет веб-поиск через DuckDuckGo и возвращает ТОП релевантных сниппетов.
        Приоритет — официальным источникам. Фильтрует низкокачественные сайты.
        Идеально подходит для RAG-систем.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru",
            "minfin.gov.ru", "who.int", "nasa.gov", "cdc.gov", "unesco.org"
        }
    
        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com"
        }
    
        for attempt in range(2):  # Повторить 1 раз при ошибке
            try:
                with DDGS(timeout=10) as ddgs:
                    results = ddgs.text(query, max_results=10)  # Берём 10 для фильтрации
                    formatted_results = []
                    official_found = False
    
                    for r in results:
                        href = r.get('href', '')
                        # Извлекаем домен
                        try:
                            domain = href.split('/')[2].lower()
                        except IndexError:
                            continue
    
                        # Пропускаем чёрный список
                        if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                            continue
    
                        # Если найден официальный источник — возвращаем только его
                        if any(official in domain for official in OFFICIAL_DOMAINS):
                            snippet = (
                                f"[ОФИЦИАЛЬНЫЙ ИСТОЧНИК]\n"
                                f"{r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            return snippet.strip()
    
                        # Собираем обычные источники, если официальных нет
                        if not official_found:
                            snippet = (
                                f"• {r['body']}\n"
                                f"Источник: {href}\n"
                            )
                            formatted_results.append(snippet)
    
                            if len(formatted_results) >= max_results:
                                break
    
                    if formatted_results:
                        return "\n".join(formatted_results).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."
    
            except Exception as e:
                if attempt == 0:
                    time.sleep(2)  # Ждём перед повтором
                    continue
                return f"Ошибка веб-поиска: {str(e)}"
    
        return "Не удалось выполнить веб-поиск. Попробуйте позже."
            
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
        return (
            f"Вопрос пользователя: {summary} \n\n"
            f"Контекст: {context_text}{extra}{web_results} \n\n"
            f"Ты — специалист по паспортному учету и регистрации граждан по месту жительства. Ответь ТОЛЬКО на основе контекста и следующих нормативов: \n\n"
            f"- Основной документ: Постановление Правительства РФ №713 «Об утверждении Правил регистрации и снятия граждан РФ с регистрационного учета по месту пребывания и по месту жительства» \n"
            f"- ФЗ №5242-1 «О праве граждан Российской Федерации на свободу передвижения, выбор места пребывания и жительства в пределах Российской Федерации» \n"
            f"- Административный регламент МВД по предоставлению государственной услуги по регистрации (Приказ МВД России №984) \n"
            f"- Каждое утверждение обязано сопровождаться ссылкой на источник: ФЗ, ПП РФ, Приказ МВД. \n"
            f"- Формат: «ФЗ №XXX-ФЗ, ст. Y» или «ПП РФ №XXX, п. Y» — строго в таком виде, без лишних слов, без кавычек вокруг номера. \n\n"
            f"ОБЯЗАТЕЛЬНО создай отдельный раздел «Судебная практика». \n\n"
            f"Формат для судебной практики: «**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда». \n\n"
            f"Если в контексте нет судебных решений, напиши: «Судебная практика по данному вопросу в базе отсутствует». \n\n"
            f"ОБЯЗАТЕЛЬНО выполни следующие шаги: \n\n"
            f"1. Если вопрос касается **места подачи документов**: \n\n"
            f"   - Укажи: 'Документы можно подать лично в отделе по вопросам миграции МВД РФ, в МФЦ или онлайн через портал Госуслуги (www.gosuslugi.ru).' \n"
            f"   - Добавь: 'Подача через Госуслуги позволяет записаться на удобное время и получить скидку 30% на госпошлину (если применимо).' \n\n"
            f"2. Если вопрос касается **необходимых документов**: \n\n"
            f"   - Перечисли: 'Для регистрации по месту жительства необходимы: паспорт, заявление по форме №6, документ-основание (свидетельство о собственности, договор найма, заявление собственника).' \n"
            f"   - Уточни: 'Если вы регистрируетесь не в своей собственности, необходимо согласие всех совершеннолетних собственников в письменной форме.' \n\n"
            f"3. Если вопрос касается **роли управляющей компании (УК)**: \n\n"
            f"   - Объясни: 'Управляющая компания НЕ оформляет прописку. Это функция органов МВД.' \n"
            f"   - Укажи: 'УК может выдать справку об отсутствии задолженности по оплате ЖКУ, если она требуется для регистрации. Срок изготовления — 3 рабочих дня (ФЗ №152-ФЗ).' \n"
            f"   - Напомни: 'После регистрации вы обязаны сообщить об этом в УК для актуализации данных по лицевому счету и корректного начисления коммунальных платежей.' \n\n"
            f"4. Если вопрос касается **сроков оформления**: \n\n"
            f"   - Укажи: 'Срок регистрации по месту жительства — 3 рабочих дня с момента подачи документов, если подача была по месту нахождения жилого помещения. Если подача была в другом регионе — до 8 рабочих дней (ПП РФ №713, п. 22).' \n\n"
            f"{self.get_role_instruction(role)} \n\n"
            f"Ассистент:[SEP]"
        )

# ---------------------------
# MetaAgent — координатор мультиагентного диалога
# ---------------------------
class MetaAgent:
    def __init__(self, rag_system):
        self.rag_system = rag_system
        self.agents = rag_system.agents
        self.dialog_log = []

    def route_intelligently(self, query: str) -> Tuple[Optional[RAGAgent], List[RAGAgent]]:
        """
        Интеллектуальная маршрутизация запроса.
        Возвращает кортеж: (основной агент, список вспомогательных агентов для консультации)
        """
        # Находим всех кандидатов, чьи ключевые слова совпадают с запросом
        primary_candidates = [a for a in self.agents if a.matches(query) and not isinstance(a, FallbackAgent)]
        secondary_candidates = []

        # Если нет кандидатов, возвращаем Fallback
        if not primary_candidates:
            fallback = next((a for a in self.agents if isinstance(a, FallbackAgent)), None)
            return fallback, []

        # Выбираем основного агента по количеству совпадений ключевых слов
        def match_score(agent: RAGAgent, qry: str) -> int:
            q_words = set(re.findall(r'\b[а-яёa-z0-9]+\b', qry.lower()))
            return sum(1 for kw in agent.keywords if kw in q_words)

        primary_agent = max(primary_candidates, key=lambda a: match_score(a, query))

        # Определяем вспомогательных агентов на основе типа основного агента
        # Это эвристика, основанная на типичных комбинациях вопросов из FAQ

        if isinstance(primary_agent, EmergencyAgent):
            # При аварии часто нужны данные по подрядчикам и контролю качества (для перерасчёта)
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (ContractorAgent, QualityControlAgent, WasteManagementAgent))
            ]

        elif isinstance(primary_agent, TariffAgent):
            # При вопросах по начислениям часто нужны данные по приборам учёта и аудиту
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (MeterAgent, BillingAuditAgent, PaymentDocumentsAgent))
            ]

        elif isinstance(primary_agent, WasteManagementAgent):
            # Вывоз ТКО часто связан с нормативами и раскрытием информации
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, DisclosureAgent, ContractorAgent))
            ]

        elif isinstance(primary_agent, TechnicalAgent):
            # Технические вопросы могут требовать нормативной базы или данных по капремонту
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, CapitalRepairAgent, QualityControlAgent))
            ]

        elif isinstance(primary_agent, LegalClaimsAgent):
            # Юридические претензии требуют знания нормативов и практики взыскания
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, DebtManagementAgent, TariffAgent))
            ]

        # 🆕 ДОБАВЛЕНО: Если основной агент — ContractorAgent, подключаем HistoryAgent
        elif isinstance(primary_agent, ContractorAgent):
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (HistoryAgent, QualityControlAgent))
            ]

        # Убираем основного агента из списка вспомогательных (если вдруг попал)
        secondary_candidates = [a for a in secondary_candidates if a != primary_agent]

        return primary_agent, secondary_candidates

    def route(self, query: str, exclude_agent: RAGAgent = None) -> Optional[RAGAgent]:
        """
        Устаревший метод для обратной совместимости.
        Просто возвращает основного агента.
        """
        primary, _ = self.route_intelligently(query)
        return primary if primary != exclude_agent else None

    def should_consult_others(self, agent: RAGAgent, query: str) -> bool:
        """
        Устаревший метод. Теперь логика координации полностью в route_intelliginely.
        Возвращает True, если есть хотя бы один вспомогательный агент.
        """
        _, secondary = self.route_intelligently(query)
        return len(secondary) > 0

    def log_dialog(self, main_agent: str, consulted_agents: List[str], final_answer: str, query: str):
        """
        Логирует результат мультиагентного диалога.
        """
        entry = {
            "timestamp": time.time(),
            "query": query,
            "main_agent": main_agent,
            "consulted_agents": consulted_agents,
            "final_answer": final_answer[:500]  # Ограничиваем длину для лога
        }
        self.dialog_log.append(entry)
        # Сохраняем лог в файл
        try:
            with open("multi_agent_log.json", "w", encoding="utf-8") as f:
                json.dump(self.dialog_log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка при сохранении лога диалога: {e}")
            
# ---------------------------
# Основной RAGSystem с поддержкой агентов
# ---------------------------

class RAGSystem:
    def __init__(self):
        self.embedding_model = embedding_model
        self.tokenizer = tokenizer
        self.model = model
        self.index = index
        self.chunks_data = chunks_data
        self.model_ctx_tokens = 32768
        self.max_context_tokens = int(self.model_ctx_tokens * 0.6)
        self.chunk_embeddings = None
        self.enable_clarification = False

        self.agents = [
            TariffAgent(),
            NormativeAgent(),
            TechnicalAgent(),
            MeterAgent(),
            DebtAgent(),
            DisclosureAgent(),
            IoTAgent(),
            MeetingAgent(),
            CapitalRepairAgent(),
            EmergencyAgent(),
            ContractorAgent(),
            HistoryAgent(),
            QualityControlAgent(),
            PaymentDocumentsAgent(),
            AccountManagementAgent(),
            BillingAuditAgent(),
            SubsidyAndBenefitsAgent(),
            LegalClaimsAgent(),
            ContractAndMeetingAgent(),
            DebtManagementAgent(),
            IoTIntegrationAgent(),
            WasteManagementAgent(),
            RegionalMunicipalAgent(),
            CourtPracticeAgent(),
            LicensingControlAgent(),
            RSOInteractionAgent(),
            SafetySecurityAgent(),
            EnergyEfficiencyAgent(),
            ReceiptProcessingAgent(),
            PassportRegistrationAgent(),
            FallbackAgent()
        ]

        self.meta_agent = MetaAgent(self)

    def detect_user_role(self, query: str) -> str:
        """
        Улучшенное определение роли пользователя на основе контекста и ключевых фраз.
        """
        text = query.lower()
    
        # Приоритет 1: Явные фразы-маркеры
        if any(phrase in text for phrase in [
            "я собственник", "я житель", "моя квартира", "мой дом", "мне начислили", "я хочу узнать",
            "как мне", "для меня", "мой лицевой счет", "мои показания", "я проживаю"
        ]):
            return "житель"
    
        if any(phrase in text for phrase in [
            "мы как ук", "мы тсн", "наша компания", "начисляем", "передаем рсо", "акт сверки с рсо",
            "расчет с рсо", "должны заплатить рсо", "исполнитель", "начислятор", "наш дом", "наши жильцы"
        ]):
            return "исполнитель"
    
        # Приоритет 2: Контекстный анализ (если явных маркеров нет)
        # Считаем баллы
        resident_score = sum([
            2 if "мой" in text or "мне" in text else 0,
            1 if "пересчитайте" in text else 0,
            1 if "почему так много" in text else 0,
            1 if "как оплатить" in text else 0,
            1 if "вызовите мастера" in text else 0,
        ])
    
        executor_score = sum([
            2 if "мы" in text and ("ук" in text or "тсн" in text or "компания" in text) else 0,
            1 if "начисляем" in text else 0,
            1 if "рсо" in text and ("передаем" in text or "платим" in text) else 0,
            1 if "акт сверки" in text else 0,
            1 if "расчет" in text and "жильцам" in text else 0,
        ])
    
        if resident_score > executor_score:
            return "житель"
        elif executor_score > resident_score:
            return "исполнитель"
        else:
            return "смешанная"

    def _encode_texts(self, texts: List[str], prompt_name: str) -> np.ndarray:
        emb = self.embedding_model.encode(texts, prompt_name=prompt_name, convert_to_numpy=True, normalize_embeddings=True)
        return emb.astype('float32')

    def _preprocess_query(self, query: str) -> str:
        tokens = re.findall(r"\w+|\S", query.lower())
        filtered = [t for t in tokens if t.isalpha() and t not in STOP_WORDS or not t.isalpha()]
        cleaned = " ".join(filtered).strip()
        return cleaned if cleaned else query

    def precompute_chunk_embeddings(self, batch_size: int = 256):
        if not self.chunks_data:
            return
        texts = [c['content'] for c in self.chunks_data]
        embs = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            embs.append(self._encode_texts(batch, prompt_name="search_document"))
        self.chunk_embeddings = np.vstack(embs)

    def analyze_query_for_clarification(self, original_query: str) -> Tuple[bool, Optional[str], Optional[str]]:
        if not self.enable_clarification:
            return False, None, original_query
        if not original_query.strip():
            return False, None, original_query

        analysis_prompt = (
            f"Пользователь: {original_query}\n\n"
            f"Инструкция:\n"
            f"Ты — юрист по вопросам ЖКХ. Анализируй запрос и формируй резюме или уточняющий вопрос строго на основании документов.\n"
            f"Если запрос ясен — формулируй summary. Если неясен — задавай уточняющий вопрос.\n"
            f"Ассистент:[SEP]"
        )

        try:
            inputs = self.tokenizer(analysis_prompt, return_tensors="pt", truncation=True, max_length=512).to(device)
            with torch.no_grad():
                outputs = self.model.generate(**inputs, max_new_tokens=150, temperature=0.1, do_sample=False, pad_token_id=self.tokenizer.eos_token_id)
            raw_analysis = self.tokenizer.decode(outputs[0], skip_special_tokens=False)
            start_marker = "Ассистент:[SEP]"
            start = raw_analysis.find(start_marker)
            if start != -1:
                analysis_part = raw_analysis[start + len(start_marker):].strip()
            else:
                analysis_part = raw_analysis.strip()
            for stop in ["</s>", "Пользователь:", "\n\n"]:
                pos = analysis_part.find(stop)
                if pos != -1:
                    analysis_part = analysis_part[:pos].strip()

            if analysis_part.startswith("Уточните, пожалуйста,"):
                question = analysis_part[len("Уточните, пожалуйста,"):].strip().rstrip("?") + "?"
                return True, question, None
            elif "Вопрос об:" in analysis_part or "Запрос о" in analysis_part:
                summary = analysis_part.replace("Вопрос об:", "").replace("Запрос о", "").strip()
                return False, None, summary
            else:
                return True, "Уточните, пожалуйста, суть вашей проблемы по ЖКХ.", None

        except Exception as e:
            print(f"❌ Ошибка анализа вопроса: {e}")
            return True, "Не удалось понять запрос. Пожалуйста, переформулируйте его.", None

    def search_relevant_chunks(self, query: str, role: str = "смешанная", top_k: int = 5) -> List[Dict]:
        if self.index is None or not self.chunks_data:
            return []
    
        query_vector = self._encode_texts([query], prompt_name="search_query")
        scores, indices = self.index.search(query_vector, top_k * 3)  # Берем больше кандидатов для фильтрации
        results = []
        for idx, score in zip(indices[0], scores[0]):
            if idx == -1: continue
            chunk = self.chunks_data[idx].copy()
            chunk["score"] = float(score)
            results.append(chunk)
    
        # НОВЫЙ БЛОК: Контекстный бустинг по тегам и ключевым словам
        q_lower = query.lower()
    
        # Определяем основную тему запроса
        theme_boost = {
            "авария": 1.5 if any(kw in q_lower for kw in ["авария", "прорыв", "затопило", "отключили", "срочно"]) else 1.0,
            "тариф": 1.5 if any(kw in q_lower for kw in ["тариф", "начисление", "плата", "стоимость", "перерасчет"]) else 1.0,
            "счетчик": 1.5 if any(kw in q_lower for kw in ["счетчик", "ипу", "одпу", "поверка", "показания"]) else 1.0,
            "тко": 1.5 if any(kw in q_lower for kw in ["тко", "мусор", "вывоз", "контейнер", "мусорная площадка"]) else 1.0,
            "собрание": 1.5 if any(kw in q_lower for kw in ["собрание", "осс", "голосование", "протокол"]) else 1.0,
        }
    
        for r in results:
            tags = [t.lower() for t in r.get("tags", [])]
            content = r.get('content', '').lower()
    
            # Бустинг по тегам
            for theme, boost in theme_boost.items():
                if theme in tags or theme in content:
                    r["score"] *= boost
    
            # Бустинг по роли (оставляем, но делаем менее агрессивным)
            if role == "житель":
                if "пп рф" in content or "фз" in content: r["score"] *= 1.1
                if "вс рф" in content: r["score"] *= 0.95
            elif role == "исполнитель":
                if "вс рф" in content or "арбитраж" in content: r["score"] *= 1.2
                if "пп рф" in content: r["score"] *= 1.1
    
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def _truncate_context_by_tokens(self, chunks_with_scores: List[Tuple[dict, float]], max_tokens_est: int):
        chunks_with_scores.sort(key=lambda x: -x[1])
        out, total = [], 0
        for chunk, score in chunks_with_scores:
            content = chunk.get('content', '').strip()
            token_count = estimate_tokens(content)
            if total + token_count > max_tokens_est:
                if total == 0:
                    sentences = sent_tokenize(content)
                    for sent in sentences:
                        sent_tokens = estimate_tokens(sent)
                        if total + sent_tokens > max_tokens_est: break
                        out.append(({'content': sent, 'source_file': chunk.get('source_file')}, score))
                        total += sent_tokens
                break
            else:
                out.append((chunk, score))
                total += token_count
        return out

    def ensure_key_cases(self, query: str, context_chunks: List[Tuple[dict, float]]) -> List[Tuple[dict, float]]:
        themes = {
            "ГВС": ["гвс", "одпу", "подогрев", "тепловая энергия"],
            "капремонт": ["капремонт", "фонд капитального ремонта"],
            "ИПУ": ["ипу", "индивидуальный прибор учета", "счетчик", "поверка"],
            "ОДН": ["одн", "общедомовые нужды"],
            "долги": ["задолженность", "долг", "неуплата"]
        }
        q_lower = query.lower()
        matched_themes = set()
        for theme, kws in themes.items():
            if any(kw in q_lower for kw in kws):
                matched_themes.add(theme)
        if matched_themes:
            for c in self.chunks_data:
                tags = [t.lower() for t in c.get("tags", [])]
                if any(t in [m.lower() for m in matched_themes] for t in tags):
                    if c not in [x[0] for x in context_chunks]:
                        context_chunks.append((c, 0.95))
        return context_chunks

    def _sanitize_answer(self, answer: str, context_text: str) -> str:
        answer = answer.replace("[NL]", "")
    
        # Удаляем ссылки и телефоны, отсутствующие в контексте
        urls = re.findall(r'https?://\S+|www\.\S+', answer)
        for u in urls:
            if u not in context_text: answer = answer.replace(u, "[ссылка отсутствует в контексте]")
    
        phones = re.findall(r'(?:(?:\+7|8)\s?[\(\-]?\d{3}[\)\-]?\s?\d{3}[\- ]?\d{2}[\- ]?\d{2})', answer)
        for p in phones:
            if p not in context_text: answer = answer.replace(p, "[телефон отсутствует в контексте]")
    
        # НОВАЯ ПРОВЕРКА: Блокировка галлюцинаций
        hallucination_triggers = [
            "я не знаю", "не могу ответить", "этого нет в документах", "извините",
            "к сожалению", "увы", "к сожалению, я не могу", "не имею информации"
        ]
    
        if any(trigger in answer.lower() for trigger in hallucination_triggers):
            return (
                "⚠️ Похоже, в моей базе знаний пока нет точной информации по вашему запросу. "
                "Пожалуйста, переформулируйте вопрос или обратитесь в управляющую компанию напрямую. "
                "Я учусь на каждом вашем запросе!"
            )
    
        # Проверка на противоречие ключевым фактам из контекста
        # (Опционально, для продвинутых систем)
        # if "не относится к зоне ответственности ук" in context_text.lower() and "ук обязана" in answer.lower():
        #     return "⚠️ Обнаружено противоречие. Пожалуйста, задайте вопрос более точно."
    
        return answer.strip()

    def _llm_complete(self, prompt: str, max_tokens: int = 2048, temperature: float = 0.1) -> str:
        try:
            inputs = self.tokenizer(prompt, return_tensors="pt", truncation=True, max_length=16000).to(device)
            with torch.no_grad():
                outputs = self.model.generate(**inputs, max_new_tokens=max_tokens, temperature=temperature, top_p=0.95, do_sample=False, pad_token_id=self.tokenizer.eos_token_id)
            raw_text = self.tokenizer.decode(outputs[0], skip_special_tokens=False)
            start = raw_text.find("Ассистент:[SEP]")
            if start != -1:
                answer_part = raw_text[start + len("Ассистент:[SEP]"):].strip()
            else:
                answer_part = raw_text.strip()
            for stop in ["</s>", "Пользователь:"]:
                pos = answer_part.find(stop)
                if pos != -1:
                    answer_part = answer_part[:pos].strip()
            answer_part = answer_part.replace("[NL]", "\n")
            return answer_part
        except Exception as e:
            print(f"❌ Ошибка генерации: {e}")
            return "Извините, не удалось сгенерировать ответ."

    # ➕ Новый метод: генерация контекста для другого агента
    def generate_context_for_agent(self, query: str, agent: RAGAgent, role: str = "смешанная") -> str:
        """Генерирует контекст специально для другого агента (без полного ответа)"""
        if not self.chunks_data or self.index is None:
            return "Нет данных"
        chunks_with_scores = [(c, c.get('score', 1.0)) for c in self.search_relevant_chunks(query, role=role, top_k=5)]
        truncated = self._truncate_context_by_tokens(chunks_with_scores, 1000)
        context_text = "\n\n".join([c['content'].strip() for c, _ in truncated]) if truncated else "Нет данных"
        return context_text[:800]

    def generate_answer_chat(self, query: str, clarification: Optional[str] = None, max_tokens: int = 2048) -> str:
        if self.index is None or not self.chunks_data:
            context_text = "Нет данных в базе."
        else:
            user_role = self.detect_user_role(query)
            
            # ИСПОЛЬЗУЕМ НОВЫЙ МЕТОД route_intelligently
            primary_agent, secondary_agents = self.meta_agent.route_intelligently(query)
            if not primary_agent:
                primary_agent = self.agents[0]  # fallback
    
            print(f"🔍 Выбран основной агент: {primary_agent.name}")
            if secondary_agents:
                print(f"🤝 Вспомогательные агенты: {[a.name for a in secondary_agents]}")
    
            if isinstance(primary_agent, FallbackAgent):
                return primary_agent.generate_fallback_response(query)
    
            # Мультиагентный диалог: консультируемся со ВСЕМИ вспомогательными агентами
            consulted_agents = []
            extra_context = ""
    
            for other_agent in secondary_agents:
                consulted_agents.append(other_agent.name)
                # Запрашиваем контекст у вспомогательного агента
                extra_context += primary_agent.consult_other_agent(query, self)
    
            # Поиск релевантных чанков для основного агента
            chunks_with_scores = [(c, c.get('score', 1.0)) for c in self.search_relevant_chunks(query, role=user_role, top_k=200)]
            chunks_with_scores = self.ensure_key_cases(query, chunks_with_scores)
            ctx_budget = max(1500, min(self.max_context_tokens - (max_tokens + 512), 8000))
            truncated = self._truncate_context_by_tokens(chunks_with_scores, ctx_budget)
            context_text = "\n\n".join([c['content'].strip() for c, _ in truncated]) if truncated else "Нет данных в базе."
            context_text += extra_context
    
            # Генерация промпта и ответа
            prompt = primary_agent._build_prompt(query, context_text, role=user_role)
            raw_answer = self._llm_complete(prompt, max_tokens=max_tokens, temperature=0.3)
            final_answer = self._sanitize_answer(raw_answer, context_text)
    
            # Логируем мультиагентный диалог
            self.meta_agent.log_dialog(primary_agent.name, consulted_agents, final_answer, query)
    
            return final_answer

    def ask(self, question: str, max_tokens: int = 2048) -> str:
        return self.generate_answer_chat(question)


# ---------------------------
# Gradio UI с оценкой ответов
# ---------------------------

rag_system = RAGSystem()
print("🎉 RAG-система с обучением агентов и мультиагентным диалогом готова.")
conversation_state = {"stage": 0}

def respond(message: str, history: list, state: dict, rating: int = None) -> Tuple[str, list, dict, gr.update, gr.update]:
    new_history = history.copy()
    user_response = message.strip() if message else ""

    try:
        # Этап 0: Получение вопроса
        if state.get("stage", 0) == 0:
            needs_clarification, clarification_message, summary = rag_system.analyze_query_for_clarification(user_response)
            if needs_clarification and clarification_message:
                new_state = {
                    "stage": 1,
                    "original_query": user_response,
                    "summary": summary,
                    "clarification_question": clarification_message
                }
                new_history.append((message, clarification_message))
                return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)
            else:
                bot_message = rag_system.ask(summary or user_response)
                new_state = {"stage": 2, "last_query": summary or user_response, "last_answer": bot_message}
                new_history.append((message, bot_message))
                # Показываем рейтинг после ответа
                return "", new_history, new_state, gr.update(visible=True), gr.update(visible=True)

        # Этап 1: Уточнение вопроса
        elif state.get("stage") == 1:
            original_query = state.get("original_query", "")
            combined_query = f"{original_query} {user_response}"
            bot_message = rag_system.ask(combined_query)
            new_state = {"stage": 2, "last_query": combined_query, "last_answer": bot_message}
            new_history.append((message, f"📝 Уточнение: {user_response}"))
            new_history.append((message, bot_message))
            # Показываем рейтинг
            return "", new_history, new_state, gr.update(visible=True), gr.update(visible=True)

        # Этап 2: Получение рейтинга ИЛИ переход к новому вопросу
        elif state.get("stage") == 2:
            # Если пользователь поставил оценку — обрабатываем её
            if rating is not None:
                last_query = state.get("last_query", "")
                last_answer = state.get("last_answer", "")
                # Передаём фидбек агенту ТОЛЬКО если рейтинг >= 4
                if rating >= 4:
                    agent = rag_system.meta_agent.route(last_query)
                    if agent:
                        agent.add_feedback(last_query, last_answer, float(rating) / 5.0)  # нормализуем до 0.0-1.0
                        feedback_msg = f"🌟 Спасибо! Ваша оценка ({rating}/5) передана агенту '{agent.name}' для обучения."
                    else:
                        feedback_msg = f"🌟 Спасибо за высокую оценку ({rating}/5)!"
                else:
                    feedback_msg = "🙏 Спасибо за честную оценку. Мы постараемся стать лучше!"

                new_state = {"stage": 0}
                new_history.append(("Оценка", feedback_msg))
                # Скрываем рейтинг после отправки
                return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)

            # Если пользователь НЕ поставил оценку, но отправил НОВЫЙ вопрос — начинаем новый диалог
            elif message.strip():
                # Рекурсивно вызываем respond для нового вопроса, сбрасывая состояние
                return respond(message, history, {"stage": 0})

            # Если ни оценки, ни нового вопроса — оставляем всё как есть (теоретически не должно происходить)
            else:
                bot_message = "Пожалуйста, оцените предыдущий ответ или задайте новый вопрос."
                return message, new_history, state, gr.update(visible=True), gr.update(visible=True)

        # Любое другое непредусмотренное состояние — сбрасываем на этап 0
        else:
            new_state = {"stage": 0}
            # Рекурсивно вызываем обработку с чистого листа
            return respond(message, history, new_state)

    except Exception as e:
        bot_message = f"⚠️ Внутренняя ошибка: {e}"
        new_state = {"stage": 0}
        new_history.append((message, bot_message))
        return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)


# Создаём интерфейс
with gr.Blocks(title="RAG-Ассистент по ЖКХ с обучением и мультиагентностью") as demo:
    gr.Markdown("## 💬 Умный RAG-ассистент по ЖКХ")
    gr.Markdown("После ответа — поставьте оценку от 1 до 5 звёзд. Отзывы с **4 и 5 звёздами** используются для обучения агентов.")

    chatbot = gr.Chatbot(label="Диалог", bubble_full_width=False)
    msg = gr.Textbox(label="Ваш вопрос", placeholder="Введите текст...")
    send_button = gr.Button("Отправить")

    # Блок рейтинга (изначально скрыт)
    with gr.Row():
        rating_slider = gr.Slider(1, 5, step=1, value=5, label="Оцените ответ (1-5 звёзд)", visible=False)
        submit_rating = gr.Button("Отправить оценку", visible=False)

    clear = gr.ClearButton([msg, chatbot, rating_slider], value="Очистить")
    state = gr.State(value={"stage": 0})

    # Обработка отправки вопроса
    msg.submit(
        respond,
        inputs=[msg, chatbot, state],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )
    send_button.click(
        respond,
        inputs=[msg, chatbot, state],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )

    # Обработка отправки рейтинга
    submit_rating.click(
        respond,
        inputs=[msg, chatbot, state, rating_slider],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )

demo.launch(share=True, server_name="0.0.0.0", server_port=7860)
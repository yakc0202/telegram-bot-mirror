# NVIDIA 모델 시험 (Gemini 대체·폴백 후보)

build.nvidia.com(NIM) 무료 키로 메시지 요약 모델 후보 3개를 시험한 기록입니다. 경위는 `REQUESTS.md` 2026-10-16·10-20
NVIDIA 항목에 있습니다. 원문은 감시 채널 내용이라 넣지 않았고, 같은 메시지의 원문과 Gemini 결과는
`docs/테스트/메시지-요약-테스트.md`(미러 제외)의 같은 번호(#1~#50)에 있습니다.

## 조건

- 실행: 2026-10-20, `_call_nvidia`(OpenAI 호환 `/chat/completions`, temperature 0.2, 요청 시간 제한 30초), 호출 사이 2초.
- 입력: A 방식(잡담 판단을 요약 호출에 합친 `summarize_message(text, with_category=True)`, 요약 말투 규칙 포함) 프롬프트,
  `메시지-요약-테스트.md`와 같은 50건(정보 30 + 잡담 20).
- 비교 기준: 같은 프롬프트의 Gemini 결과(정보 30건은 말투 규칙 추가 후 결과).
- 시험 스크립트가 첫 실행에서 `call_llm` 대체 함수의 인자 이름을 잘못 써서 전부 실패 → 고쳐서 다시 실행한 결과입니다.

## 결과 요약

| 모델 | 성공 | 시간 초과(30초) | 빈 응답 | Gemini와 분류 일치(성공분) | 응답 시간 중간값 / 최대 |
|---|---|---|---|---|---|
| `google/gemma-4-31b-it` | 32/50 | 18 | 0 | 30/32 | 8.8초 / 27.3초 |
| `deepseek-ai/deepseek-v4.1-flash` | 0/50 | 50 | 0 | 0/0 | -초 / -초 |
| `z-ai/glm-5.3-flash` | 15/50 | 3 | 32 | 15/15 | 8.1초 / 19.9초 |

- Gemini는 같은 호출이 보통 1~2초입니다. 봇은 `llm_processing_lock`으로 메시지를 한 건씩 처리하므로, 한 건에 8~9초면
  뒤 메시지가 밀리고 30초 시간 초과가 잦으면 그만큼 요약 실패(원문 미리보기)로 나갑니다.
- `glm-5.3-flash`의 빈 응답은 추론(thinking) 부분이 출력 한도를 다 써서 본문이 비는 것으로 보입니다(원인은 미확인).
- 결론: 지금 상태로는 세 모델 모두 1순위로 쓰기 어렵고, 폴백으로도 `gemma-4-31b-it`만 후보(성공 64%, 느림).

## 메시지별 결과 전문

### #1 정보 — Gemini 분류: STOCK

Gemini:

```text
중동 지역 내 안보 불안 심화로 인해 미국 대사관이 미국 시민에게 보안 경보를 발령하고 항공편 운항 차질 가능성을 경고했습니다.

💡 중동 지역 내 지정학적 리스크 고조는 국제 유가 상승 압력과 항공·물류 섹터의 변동성을 키우는 요인으로 작용할 수 있습니다.
```

`google/gemma-4-31b-it` (21.8초):

```text
주예루살렘 미국 대사관이 중동 지역의 안보 상황 악화와 후티 반군의 공격 가능성으로 인해 미국 시민들에게 경계 태세 강화 및 여행 주의보를 발령했습니다.

💡 중동 지역의 지정학적 리스크 확대로 인해 국제 유가 상승 및 방산 섹터의 수혜가 예상됩니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.4초):

```text
(실패: NVIDIA 빈 응답)
```

### #2 정보 — Gemini 분류: STOCK

Gemini:

```text
모건스탠리의 마이크 윌슨은 주식시장 조정이 마무리 단계에 진입했다고 평가하며 투자자들에게 위험자산 비중 확대를 권고했습니다.

💡 S&P 500 편입 기업의 3분기 이익 증가세와 결합하여 우량주를 중심으로 한 시장 전반의 투자 심리 회복에 긍정적인 영향을 줄 것으로 예상됩니다.
```

`google/gemma-4-31b-it` (15.5초):

```text
모건스탠리의 마이크 윌슨은 주식시장 조정이 마무리 단계에 접어들었다고 분석하며 위험자산 비중 확대를 통한 매수 준비를 권고했습니다. S&P 500 기업들의 3분기 이익이 전년 대비 24.6% 증가할 것으로 예상된다는 데이터가 함께 제시되었습니다.

💡 우량주 중심의 포트폴리오 재편과 S&P 500 지수 편입 대형주들의 매수세 유입 가능성이 높아질 것으로 보입니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (19.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #3 정보 — Gemini 분류: STOCK

Gemini:

```text
미국 중간선거가 15일 앞으로 다가왔습니다.

💡 선거 결과에 따른 정책 기조 변화로 인해 관련 산업별 주가 변동성이 확대될 것으로 예상됩니다.
```

`google/gemma-4-31b-it` (7.9초):

```text
미국 중간선거가 15일 앞으로 다가왔습니다.

💡 선거 결과에 따른 정책 변화가 미국 증시 및 관련 섹터의 변동성을 확대시킬 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (25.7초):

```text
(실패: NVIDIA 빈 응답)
```

### #4 정보 — Gemini 분류: STOCK

Gemini:

```text
미국 증시 선물은 나스닥100을 중심으로 상승세를 보이고 있으나, 연준의 추가 긴축 가능성과 AI 인프라 밸류에이션 재평가 및 에너지 가격 불확실성이 상존합니다.

💡 AI 인프라 투자의 수익성 평가와 국채 금리 움직임에 따라 엔비디아를 포함한 기술주 및 반도체 관련 종목의 변동성이 확대될 것으로 예상됩니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (11.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #5 정보 — Gemini 분류: STOCK

Gemini:

```text
코어위브와 네비우스 등 AI 인프라 기업에 대한 미국 레딧 투자자들의 관심도가 상승하고 있으며 유가 변동성도 함께 주목받고 있습니다.

💡 데이터 센터 전력 수요와 직접적인 관련이 있는 AI 인프라 및 에너지 섹터의 투자 심리에 영향을 줄 수 있습니다.
```

`google/gemma-4-31b-it` (19.9초):

```text
미국 커뮤니티 레딧에서 코어위브, 네비우스, 유가에 대한 관심도가 높아지고 있습니다.

💡 AI 인프라 관련 기업 및 에너지 섹터에 대한 시장의 관심 증가가 예상됩니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (9.8초):

```text
(실패: NVIDIA 빈 응답)
```

### #6 정보 — Gemini 분류: STOCK

Gemini:

```text
골드만삭스 자료에 따르면 최근 헤지펀드들이 지난 10년 중 가장 빠른 속도로 기술주 비중을 축소하고 있습니다.

💡 기술주에 대한 투자 심리 위축과 거래량 감소로 인해 나스닥을 비롯한 대형 기술주들의 단기적인 변동성이 확대될 수 있습니다.
```

`google/gemma-4-31b-it` (22.7초):

```text
골드만삭스에 따르면 헤지펀드들이 최근 10년 만에 가장 빠른 속도로 기술주 비중을 축소했습니다.

💡 기술주 전반에 대한 투자 심리 위축으로 인해 대형 기술주 및 반도체 섹터의 단기적인 하방 압력이 커질 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (19.5초):

```text
(실패: NVIDIA 빈 응답)
```

### #7 정보 — Gemini 분류: STOCK

Gemini:

```text
센트러스 에너지가 우라늄 농축 사업 확장을 위해 원자력 부품 공급업체 웨스터먼을 1억 1,600만 달러에 인수하기로 결정했습니다.

💡 이번 인수를 통해 핵심 부품 제조 역량을 내재화하여 공급망을 안정화하고 원자력 및 에너지 관련 산업의 비용 절감과 매출 다각화에 기여할 것으로 예상됩니다.
```

`google/gemma-4-31b-it` (30.3초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.4초):

```text
(실패: NVIDIA 빈 응답)
```

### #8 정보 — Gemini 분류: STOCK

Gemini:

```text
MDS테크가 국내 기업의 휴머노이드 홈로봇 개발 프로젝트를 위해 40억 원 규모의 엔비디아 AI 플랫폼 및 센서를 공급하는 계약을 체결했습니다.

💡 엔비디아의 AI 솔루션 유통 비즈니스와 로봇 관련 기술 협력 확대를 통해 MDS테크의 외형 성장 및 관련 시장에서의 경쟁력이 강화될 것으로 전망됩니다.
```

`google/gemma-4-31b-it` (16.6초):

```text
MDS테크가 국내 기업의 휴머노이드 홈로봇 개발 프로젝트에 엔비디아 기반 AI 컴퓨팅 플랫폼과 센서를 공급하는 40억 원 규모의 계약을 체결했습니다.

💡 휴머노이드 로봇 및 AI 솔루션 관련 섹터의 모멘텀이 될 수 있으며, MDS테크의 매출 증대와 엔비디아 생태계 확장성이 부각될 것으로 보입니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.7초):

```text
MDS테크가 국내 기업의 휴머노이드 홈로봇 개발 프로젝트에 엔비디아 기반 AI 컴퓨팅 플랫폼과 센서 등을 공급하는 40억원 규모 수주를 체결했습니다.

💡 MDS테크 주가에 직접적인 호재로 작용할 수 있으며, 휴머노이드 로봇 상용화 흐름에 따라 로봇 부품·
```

### #9 정보 — Gemini 분류: STOCK

Gemini:

```text
미국 레딧 커뮤니티에서 마이크론, 금, 나스닥, 유가에 대한 투자자들의 관심도가 높은 것으로 나타났습니다.

💡 주요 자산과 원자재에 대한 개인 투자자들의 심리가 집중되면서 해당 종목과 관련 시장의 변동성이 확대될 가능성이 있습니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (32.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (15.5초):

```text
(실패: NVIDIA 빈 응답)
```

### #10 정보 — Gemini 분류: STOCK

Gemini:

```text
중국 메모리 반도체 기업 CXMT의 3분기 잠정 실적에서 순이익 증가율이 전분기 대비 대폭 둔화되고 순이익률이 6%p 하락하며 주가가 7.8% 급락했습니다.

💡 글로벌 메모리 반도체 시장의 가격 상승세 둔화와 함께 고부가가치 제품 및 HBM 생산 능력의 격차가 중국 기업의 수익성에 미치는 부정적 영향이 드러나고 있습니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (10.8초):

```text
(실패: NVIDIA 빈 응답)
```

### #11 정보 — Gemini 분류: STOCK

Gemini:

```text
미국 연방기관이 딥시크 및 관련 제3자 서비스 사용을 금지하는 조치를 발표했습니다.

💡 중국 AI 모델에 대한 보안 규제가 강화되면서 국산 AI 솔루션이나 미중 기술 패권 경쟁 관련 기업들에 반사 수혜가 나타날 수 있습니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (29.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #12 정보 — Gemini 분류: STOCK

Gemini:

```text
골드만삭스는 한국 은행주를 방어주로 긍정 평가하며 KB금융 등 5개 종목에 매수 의견을 제시했으나, 보험주와 핀테크주는 성장성과 환원 정책의 불확실성을 이유로 신중한 투자를 권고했습니다.

💡 은행 섹터의 추가적인 주가 상승을 위해서는 순이자마진(NIM) 반등과 함께 ROE 개선이 뒷받침되어야 하며, 보험 및 핀테크 섹터는 실질적인 밸류업 실행력과 주주환원 규모에 따라 주가 차별화가 진행될 것입니다.
```

`google/gemma-4-31b-it` (27.3초):

```text
골드만삭스가 KB금융(214,000원), 신한지주(123,000원), 하나금융지주(148,000원) 등 주요 은행주에 대해 매수 의견을 제시했으며, 보험주와 핀테크주는 종목별로 중립 및 매도 의견을 냈습니다.

💡 은행주는 NIM 반등 여부에 따라 추가 상승이 결정될 것이며, 보험주는 밸류업 실행력과 핀테크주는 성장 지속성이 핵심 변수가 될 것으로 보입니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (6.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #13 정보 — Gemini 분류: STOCK

Gemini:

```text
트럼프 대통령은 캔자스주 상원의원 선거에서 로저 마셜 후보를 지지하며, 그가 추진 중인 신용카드 과도한 결제 수수료 인하 법안에 서명하겠다는 의지를 밝혔습니다.

💡 신용카드사의 수익 구조 변화가 예상됨에 따라 결제망 관련주 및 금융 서비스 업종의 규제 리스크와 주가 변동성에 유의할 필요가 있습니다.
```

`google/gemma-4-31b-it` (30.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (16.0초):

```text
(실패: NVIDIA 빈 응답)
```

### #14 정보 — Gemini 분류: STOCK

Gemini:

```text
트럼프 대통령은 일론 머스크가 '트럼프 계좌'를 통해 미국 어린이들에게 스페이스X 주식을 1주씩 무상 증여할 것이라고 발표했습니다.

💡 민간 우주 산업의 상징인 스페이스X의 지분 구조 변화와 기업 가치 평가에 영향을 줄 수 있으며, 관련 우주 항공 및 위성 통신 섹터에 대한 시장의 관심이 집중될 것입니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

### #15 정보 — Gemini 분류: STOCK

Gemini:

```text
엔비디아가 투자한 데이터센터 개발사 엔스케일이 기대 기업가치 300억 달러에 대해 투자자 반발에 직면했으며, AI 인프라 업계 전반의 자금 조달 비용 상승과 수익성 우려로 주가가 하락하고 있습니다.

💡 AI 인프라 투자의 수익성 증명 여부가 중요해짐에 따라 이번 주 메타를 비롯한 빅테크의 실적 발표가 관련 AI 인프라 및 기술주 섹터의 투자 심리에 직접적인 영향을 줄 것으로 전망됩니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (7.3초):

```text
(실패: NVIDIA 빈 응답)
```

### #16 정보 — Gemini 분류: STOCK

Gemini:

```text
주요 헤지펀드 매니저들의 최근 발언을 종합한 결과 폴 튜더 존스는 증시 하락을 예상하며 유가 상승 가능성을 언급했고 레이 달리오와 제프리 건들락은 각각 AI 버블 경고와 미국 증시의 위험성을 지적했습니다.

💡 이러한 거시적 관점의 비관적 분석은 기술주 투자 심리 위축과 에너지 섹터의 변동성 확대를 초래할 수 있습니다.
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (21.0초):

```text
(실패: NVIDIA 빈 응답)
```

### #17 정보 — Gemini 분류: STOCK

Gemini:

```text
S&P 글로벌은 주요 빅테크 기업들이 현재 공개된 것보다 더 많은 AI 관련 부채를 보유하고 있을 가능성이 높다고 경고했습니다.

💡 AI 인프라 투자 비용이 급증하는 빅테크 기업들의 재무 건전성 우려를 높여 관련 기술주 전반의 투자 심리에 부정적인 영향을 줄 수 있습니다.
```

`google/gemma-4-31b-it` (20.7초):

```text
S&P 글로벌은 글로벌 빅테크 기업들의 AI 관련 부채가 현재 알려진 것보다 더 많을 가능성이 있다고 경고했습니다.

💡 AI 인프라 투자 비용 증가로 인한 빅테크 기업들의 재무 부담 가중 및 관련 하드웨어/소프트웨어 섹터의 밸류에이션 조정 가능성이 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

### #18 정보 — Gemini 분류: STOCK

Gemini:

```text
리투아니아 영공을 침범해 폭발한 드론이 우크라이나산으로 확인되었으며, 이에 따라 리투아니아 정부가 관련 조사를 진행하고 있습니다.

💡 지정학적 리스크 심화에 따라 유럽 내 방산 관련주 및 동유럽 지역의 안보 관련 불확실성이 증대될 수 있습니다.
```

`google/gemma-4-31b-it` (14.7초):

```text
리투아니아 영공을 침범해 폭발한 드론이 우크라이나산으로 확인되었다고 리투아니아 총리가 발표했습니다.

💡 지정학적 리스크 고조로 인해 방산 섹터 및 드론 관련주에 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (11.0초):

```text
리투아니아 신케비추스 총리는 일요일 리투아니아 영공을 침범한 드론이 우크라이나산 FPV 드론으로 확인됐으며 폭발물을 탑재하고 있었다고 발표했습니다. 해당 드론은 폴란드에서 진입해 파스발리스 지역에서 폭발해 민간 차량 1대가 파손됐습니다.

💡 나토 회원국 영공 침범과 폭발
```

### #19 정보 — Gemini 분류: STOCK

Gemini:

```text
바이오텍 베라더믹스의 경구용 탈모 치료제 VDPHL01이 임상 3상에서 위약 대비 유의미한 모발 개선 효과를 입증했습니다.

💡 탈모 치료제 시장의 미충족 수요가 높은 만큼, 이번 임상 성공은 관련 제약·바이오 섹터 전반에 대한 투자 심리 개선과 신규 치료제 개발 경쟁을 촉진할 것으로 전망됩니다.
```

`google/gemma-4-31b-it` (8.2초):

```text
베라더믹스의 경구용 미녹시딜 VDPHL01이 임상 3상에서 위약 대비 뚜렷한 모발 개선 효과를 입증했으며, 향후 FDA 허가 신청을 계획하고 있습니다.

💡 탈모 치료제 시장의 신규 진입으로 인해 베라더믹스를 포함한 탈모 치료 관련 바이오텍 및 헬스케어 섹터에 긍정적인 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (10.7초):

```text
(실패: NVIDIA 빈 응답)
```

### #20 정보 — Gemini 분류: STOCK

Gemini:

```text
미국 에너지부 자료에 따르면 미국 전략비축유 원유 재고가 2억 8,270만 배럴로 감소하며 1982년 10월 이후 최저치를 기록했습니다.

💡 원유 공급 부족 우려를 자극하여 국제 유가 상승 압력으로 작용할 수 있으며 에너지 및 정유 관련 섹터에 영향을 줄 수 있습니다.
```

`google/gemma-4-31b-it` (6.3초):

```text
미국 전략비축유 원유 재고가 2억 8,270만 배럴로 감소하며 1982년 10월 이후 최저 수준을 기록했습니다.

💡 원유 재고 감소에 따른 공급 부족 우려로 국제 유가 및 에너지 관련 종목에 상승 요인이 될 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (17.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #21 정보 — Gemini 분류: STOCK

Gemini:

```text
중국 상무부가 12월 18일부터 국가 안보를 이유로 터널굴착기 및 보행식 굴착기 등 건설 장비와 관련 핵심 기술을 수출 통제 대상에 포함했습니다.

💡 중국산 굴착 장비 공급이 제한됨에 따라 글로벌 건설 현장에서 국산 건설기계 장비나 대체 기술을 보유한 기업들의 반사이익 가능성이 있습니다.
```

`google/gemma-4-31b-it` (10.3초):

```text
중국 상무부가 국가안보를 이유로 전단면 터널굴착기(TBM)와 보행식 굴착기 등 핵심 굴착 장비 및 부품의 수출을 통제하며, 내달 18일부터 시행합니다.

💡 중국산 굴착 장비의 공급 제한으로 인해 국내외 TBM 및 특수 굴착 장비 제조 기업들의 반사이익이 기대됩니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (4.8초):

```text
(실패: NVIDIA 빈 응답)
```

### #22 정보 — Gemini 분류: STOCK

Gemini:

```text
미 증시는 테크주 반등으로 하락분을 일부 만회했으나, 고객예탁금이 94조 원으로 감소하고 NAHB 주택시장지수가 29로 하락하며 투자 심리 위축과 경기 둔화 우려가 지속되고 있습니다.

💡 고객예탁금 감소는 증시 내 유동성 축소를 의미하며, 주택시장지표 악화는 건설 및 주택 관련 섹터에 부정적인 영향을 줄 것으로 전망됩니다.
```

`google/gemma-4-31b-it` (6.2초):

```text
미 증시는 테크주 저점 인식으로 하락분의 절반을 회복했으며, 국내 고객예탁금은 94조 원으로 9월 이후 최저치를 기록하고 NAHB 주택시장 지수는 29로 급락했습니다.

💡 미국 주택시장 위축과 국내 투자 심리 악화가 지수 하방 압력으로 작용할 수 있으며, 테크 섹터의 반등 여부가 시장의 핵심 변수가 될 것으로 보입니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (4.3초):

```text
(실패: NVIDIA 빈 응답)
```

### #23 정보 — Gemini 분류: STOCK

Gemini:

```text
비나텍의 공급계약 체결과 삼성전자SDI의 전고체 배터리 양산 계획 등 개별 기업의 호재가 확인되나, 금감원장의 SK하이닉스 솔리다임 중복상장 우려 제기로 반도체 업종 전반에 관망세가 짙습니다.

💡 배터리 및 로봇 관련주는 기술 개발과 수주 모멘텀으로 강세를 보이나, 반도체 섹터는 정책적 리스크 및 수급 불안으로 인해 투자 심리가 위축될 가능성이 높습니다.
```

`google/gemma-4-31b-it` (9.9초):

```text
비나텍이 1,230억 규모 공급계약을 체결했고, 삼성SDI의 전고체 배터리 양산 추진, 파두와 엘앤씨바이오의 실적 개선 소식이 있습니다. 반면 SK하이닉스는 솔리다임의 중복상장 우려와 외국인 매도로 약세를 보이고 있습니다.

💡 AI, 휴머노이드 로봇, 2차전지 및 ESS 섹터의 긍정적 흐름이 예상되며, 반도체 소부장 및 양자컴퓨팅 관련 종목의 수혜가 기대됩니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (8.2초):

```text
(실패: NVIDIA 빈 응답)
```

### #24 정보 — Gemini 분류: STOCK

Gemini:

```text
이찬진 금융감독원장이 SK하이닉스의 자회사인 솔리다임의 미국 상장 추진에 따른 중복 상장 우려를 회사 측에 전달하는 방안을 검토하겠다고 밝혔습니다.

💡 자회사 분할 상장 이슈는 모회사인 SK하이닉스의 지주사 할인 및 기업 가치에 부정적인 영향을 미칠 수 있어 향후 의사 결정 과정이 주가 변동 요인으로 작용할 수 있습니다.
```

`google/gemma-4-31b-it` (4.6초):

```text
이찬진 금융감독원장이 솔리다임의 미국 상장 추진에 따른 중복 상장 우려를 모회사인 SK하이닉스에 전달하겠다고 밝혔습니다.

💡 SK하이닉스의 기업 가치 희석 우려 및 솔리다임의 상장 추진 방향에 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #25 정보 — Gemini 분류: STOCK

Gemini:

```text
코스피 야간선물이 상승세를 보이고 있습니다.

💡 코스피 지수가 다음 날 정규장에서 상승 출발할 가능성을 시사합니다.
```

`google/gemma-4-31b-it` (1.6초):

```text
코스피 야간선물이 상승세를 보이고 있습니다.

💡 국내 증시 개장 시 긍정적인 영향을 줄 가능성이 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (8.0초):

```text
(실패: NVIDIA 빈 응답)
```

### #26 정보 — Gemini 분류: STOCK

Gemini:

```text
푸틴 러시아 대통령이 트럼프 미국 대통령에게 우크라이나 전쟁 종식을 위한 미·중·러 3자 정상회담을 제안하였습니다.

💡 러시아-우크라이나 전쟁의 지정학적 리스크 완화 기대감에 따라 에너지 및 방산 섹터의 변동성이 확대될 수 있습니다.
```

`google/gemma-4-31b-it` (4.6초):

```text
푸틴 러시아 대통령이 우크라이나 전쟁 종결을 위해 트럼프 대통령과 시진핑 주석이 참여하는 미·중·러 3자 정상회담을 제안했습니다.

💡 전쟁 종식 기대감으로 인해 우크라이나 재건 관련주 및 글로벌 지정학적 리스크 감소에 따른 시장 안정화에 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (6.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #27 정보 — Gemini 분류: STOCK

Gemini:

```text
JAXA와 미츠비시중공업이 공동 개발한 H3 로켓 10호기가 발사에 성공하며 화성 위성 탐사선 MMX를 궤도에 진입시켰습니다.

💡 우주 항공 산업 분야의 기술적 신뢰도 향상에 따라 관련 부품을 공급하는 항공우주 및 방위산업 섹터 기업들의 수주 기대감이 높아질 수 있습니다.
```

`google/gemma-4-31b-it` (14.8초):

```text
JAXA와 미츠비시중공업이 개발한 H3 로켓 10호기가 발사에 성공하여 화성 위성 탐사선 MMX를 궤도에 진입시켰습니다.

💡 우주 항공 산업 및 미츠비시중공업을 포함한 관련 부품 공급망 기업들에 긍정적인 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (16.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #28 정보 — Gemini 분류: STOCK

Gemini:

```text
아이티센글로벌의 강진모 회장이 지난 10월 16일과 19일 이틀에 걸쳐 총 6,190주를 장내 매수하여 지분율이 기존 12.31%에서 12.34%로 증가했습니다.

💡 최대주주 및 경영진의 자사주 매입은 책임 경영 의지를 시장에 전달하여 주가 방어 및 투자 심리 회복에 긍정적인 신호로 작용할 수 있습니다.
```

`google/gemma-4-31b-it` (2.7초):

```text
아이티센글로벌의 강진모 회장이 10월 16일과 19일 양일에 걸쳐 보통주 총 6,190주를 장내 매수하여 지분율이 12.34%로 상승했습니다.

💡 대주주의 장내 매수는 경영권 강화 및 주가 부양 의지로 해석되어 해당 종목에 긍정적인 영향을 줄 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

### #29 정보 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (0.5초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (22.0초):

```text
(실패: NVIDIA 빈 응답)
```

### #30 정보 — Gemini 분류: STOCK

Gemini:

```text
2026년 10월 19일 뉴욕증시는 아마존을 비롯한 BIG7 종목의 주가 흐름과 유가 변동, 금리 환경이 주요 변수로 작용하며 마감되었습니다.

💡 아마존의 AI 모멘텀이 기업 실적에 미치는 영향과 함께 AI 관련 신용 위험 요소가 기술주 전반의 변동성을 확대할 가능성이 있습니다.
```

`google/gemma-4-31b-it` (1.8초):

```text
뉴욕증시 마감 시황에서 빅7 기업들의 동향과 유가, 금리, 아마존의 AI 모멘텀 및 AI 신용 위험이 다뤄졌습니다.

💡 AI 산업의 신용 위험과 모멘텀에 따라 빅테크 기업 및 AI 관련 섹터의 주가 변동성이 커질 수 있습니다.
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (6.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #31 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (2.4초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #32 잡담 — Gemini 분류: OPINION

Gemini:

```text
(요약 없음 — OPINION)
```

`google/gemma-4-31b-it` (4.7초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (5.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #33 잡담 — Gemini 분류: OPINION

Gemini:

```text
(요약 없음 — OPINION)
```

`google/gemma-4-31b-it` (9.4초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (7.0초):

```text
(실패: NVIDIA 빈 응답)
```

### #34 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (18.8초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (10.6초):

```text
(요약 없음 — CHITCHAT)
```

### #35 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (8.1초):

```text
(요약 없음 — CHITCHAT)
```

### #36 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (2.3초):

```text
(요약 없음 — CHITCHAT)
```

### #37 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (17.9초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (17.1초):

```text
(실패: NVIDIA 빈 응답)
```

### #38 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (4.0초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (19.9초):

```text
(요약 없음 — CHITCHAT)
```

### #39 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (28.1초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (10.4초):

```text
(요약 없음 — CHITCHAT)
```

### #40 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (4.7초):

```text
(요약 없음 — CHITCHAT)
```

### #41 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (7.9초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (17.4초):

```text
(요약 없음 — CHITCHAT)
```

### #42 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (4.1초):

```text
(요약 없음 — CHITCHAT)
```

### #43 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (25.0초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (4.8초):

```text
(요약 없음 — CHITCHAT)
```

### #44 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (11.9초):

```text
(요약 없음 — CHITCHAT)
```

### #45 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (17.1초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (11.4초):

```text
(요약 없음 — CHITCHAT)
```

### #46 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (2.0초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (16.9초):

```text
(실패: NVIDIA 빈 응답)
```

### #47 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (16.2초):

```text
(실패: NVIDIA 빈 응답)
```

### #48 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (5.8초):

```text
(요약 없음 — CHITCHAT)
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (25.4초):

```text
(실패: NVIDIA 빈 응답)
```

### #49 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (6.9초):

```text
(요약 없음 — CHITCHAT)
```

### #50 잡담 — Gemini 분류: CHITCHAT

Gemini:

```text
(요약 없음 — CHITCHAT)
```

`google/gemma-4-31b-it` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`deepseek-ai/deepseek-v4.1-flash` (30.2초):

```text
(실패: HTTPSConnectionPool(host='integrate.api.nvidia.com', port=443): Read timed out. (read timeout=30))
```

`z-ai/glm-5.3-flash` (2.9초):

```text
(요약 없음 — CHITCHAT)
```


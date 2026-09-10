# Claude Code prompt — LEAP 앱에 crawler_items 연결

아래 프롬프트를 Claude Code에서 **LKA09/LEAP 저장소 루트**를 연 상태로 그대로 사용하세요.

---

너는 현재 LEAP 웹앱 저장소를 수정한다. 먼저 저장소 전체 구조와 기존 구현 패턴을 읽고, 기존 기능을 깨지 않는 최소 변경으로 작업해라.

## 현재 시스템 전제

- 앱 저장소: `LKA09/LEAP`
- Next.js 15 App Router + React + TypeScript
- Firebase Auth + Firestore 클라이언트 SDK 사용
- 정적 export 기반 배포
- 기존 펀딩/가입/관리자/채팅/결과 기능은 절대 회귀시키지 말 것
- Firebase 프로젝트는 `leap-9e2ec`
- Firestore 보안 규칙은 `firestore.rules`가 생성물이다. **직접 규칙 파일만 손대지 말고 `scripts/generate-rules.mjs`를 수정한 뒤 생성 결과를 갱신할 것.**
- 별도 public repo `LKA09/Leap_crawler`의 GitHub Actions가 Firebase Admin SDK로 데이터를 수집한다.
- 크롤러의 서비스 계정이나 GitHub Secret은 프런트엔드에 절대 넣지 않는다.

## Firestore에서 크롤러가 쓰는 데이터

컬렉션: `crawler_items`

각 문서는 대략 다음 shape이다.

```ts
{
  title?: string;
  url?: string;
  summary?: string;
  imageUrl?: string;
  publishedAt?: string;
  sourceKey: string;
  sourcePage: string;
  contentHash: string;
  createdAt: Timestamp;
  updatedAt: Timestamp;
}
```

문서 ID는 크롤러가 `sourceKey + item identity`를 SHA-256으로 만든 안정적인 ID다. 앱에서 문서 ID를 새로 계산하지 말고 snapshot.id를 그대로 사용한다.

`crawler_runs/{sourceKey}`는 크롤러 내부 상태용이며 프런트엔드에서 읽지 않는다.

## 구현 목표

### 1. 타입과 데이터 접근 계층

기존 `src/lib/types.ts` 및 lib 패턴을 확인해서 스타일을 맞춘다.

`CrawlerItem` 타입을 추가하되 Firestore Timestamp 타입을 UI 타입 전체에 강하게 전파하지 않는 방향을 선호한다. 최소한 다음 정보는 앱에서 안전하게 사용할 수 있어야 한다.

- id
- title
- url
- summary
- imageUrl
- publishedAt
- sourceKey
- updatedAt

`src/lib/useCrawlerItems.ts` 같은 전용 hook/data layer를 추가한다.

요구사항:

- `crawler_items`를 읽기 전용으로 구독
- 최신 `updatedAt` 순으로 최대 100개
- loading / error / items 상태 제공
- 문서 데이터가 일부 누락돼도 화면 전체가 깨지지 않게 defensive parsing
- URL은 `https://` 또는 `http://`인 경우만 외부 링크로 사용
- imageUrl은 `https://`인 경우만 이미지로 사용
- `dangerouslySetInnerHTML` 사용 금지
- 클라이언트에서 Firestore write 기능 추가 금지
- 불필요한 실시간 listener가 중복 생성되지 않게 cleanup 처리

Firestore 쿼리 때문에 별도 composite index가 필요하지 않도록 단순한 `orderBy('updatedAt', 'desc') + limit(100)` 구조를 우선 사용한다.

### 2. UI 연결

기존 라우팅과 디자인을 먼저 확인해라.

이미 외부 자료/소식/인사이트를 넣기 적합한 화면이 있으면 그 화면에 자연스럽게 연결한다. 그런 화면이 전혀 없다면 `/feed` 라우트를 새로 만들고 기존 Header/Nav 패턴에 맞는 링크를 최소 변경으로 추가한다.

새 화면을 만드는 경우:

- 페이지 이름: `외부 자료`
- 카드 목록 형태
- title 우선 표시
- summary가 있으면 2~3줄 미리보기
- imageUrl이 유효하면 썸네일, 없으면 레이아웃 유지
- sourceKey를 작은 보조 텍스트로 표시
- publishedAt이 있으면 사람이 읽기 좋은 날짜로 표시
- url이 유효하면 `새 탭에서 보기` 외부 링크
- `target="_blank"` 사용 시 `rel="noopener noreferrer"` 반드시 추가
- loading / empty / error UI 구현
- 모바일에서도 기존 앱처럼 정상 표시
- 기존 CSS Modules 스타일을 재사용하고 새 스타일도 CSS Module로 작성
- 기존 펀딩 화면의 색상/간격/버튼 스타일과 시각적으로 충돌하지 않게 할 것

크롤링된 원문 전체 HTML을 화면에 렌더링하지 않는다. 현재 schema의 텍스트 필드만 사용한다.

### 3. Firestore Security Rules

크롤러는 Firebase Admin SDK로 쓰기 때문에 클라이언트 write 권한이 필요 없다.

`scripts/generate-rules.mjs`를 수정해서 생성되는 규칙에 다음 정책을 추가한다.

```text
match /crawler_items/{id} {
  allow read: if true;
  allow write: if false;
}

match /crawler_runs/{id} {
  allow read, write: if false;
}
```

`crawler_items`는 데모에서 로그인 전에도 보여줄 수 있는 공개 크롤링 데이터로 취급한다. 기존 보안 규칙의 다른 collection 권한은 바꾸지 않는다.

생성기를 수정한 뒤 프로젝트가 사용하는 기존 명령/빌드 흐름을 통해 `firestore.rules`도 최신 상태로 갱신한다.

### 4. 테스트

기존 테스트 구조를 확인하고 가능한 범위에서 다음을 검증한다.

- CrawlerItem defensive parsing 또는 URL validation
- 빈 collection/누락 필드 처리
- 기존 테스트가 전부 통과하는지
- `npm run build` 성공
- Firestore rules 관련 기존 테스트가 있으면 crawler collection 규칙도 추가 검증
  - `crawler_items` client read 허용
  - `crawler_items` client create/update/delete 거부
  - `crawler_runs` client read/write 거부

실제 운영 Firestore 데이터를 수정하는 테스트는 하지 않는다. 기존 emulator 기반 테스트 패턴이 있으면 그대로 따른다.

## 중요한 제한

- Firebase Admin SDK를 LEAP 프런트엔드에 설치하거나 사용하지 말 것.
- 서비스 계정 private key를 코드, `.env.example`, README 등에 넣지 말 것.
- 기존 Firebase client config 구조를 임의로 변경하지 말 것.
- 기존 펀딩 트랜잭션/명단 인증/관리자 권한/결과 마감 로직을 리팩터링하지 말 것.
- unrelated 파일 formatting 대량 변경 금지.
- 새로운 상태관리 라이브러리 추가 금지.
- 현재 프로젝트에 이미 있는 Firebase SDK와 React 패턴을 재사용할 것.

## 작업 완료 시 보고 형식

마지막에 다음을 명확히 보고해라.

1. 수정/추가한 파일 목록
2. 데이터 흐름: `GitHub Actions crawler -> Firestore crawler_items -> LEAP UI`
3. Firestore 규칙 변경 내용
4. 실행한 테스트와 결과
5. 내가 수동으로 해야 할 일이 있다면 정확한 명령과 Firebase/GitHub 설정 위치
6. 발견한 기존 문제는 이번 작업 범위와 직접 관련된 것만 별도 표시

코드를 먼저 충분히 읽고 기존 스타일에 맞춰 구현한 다음, 실제 build/test까지 수행해라. 구현 중 애매한 부분은 기존 코드의 convention을 우선하고, 기능을 임의로 크게 확장하지 마라.

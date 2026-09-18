// Phase 2P.1C — pure N-page navigation logic (no React, no @decky/ui).
// Kept dependency-free so the lightweight Node harness can unit-test it.
//
// Shoulder navigation is data-driven and non-wrapping:
//   L1 = previous page, R1 = next page
//   first page + L1 -> stay first; last page + R1 -> stay last.

export type QamPage = {
  id: string;
  title: string;
};

export function clampPageIndex(index: number, pageCount: number): number {
  if (!Number.isFinite(index) || pageCount <= 0) {
    return 0;
  }
  return Math.min(Math.max(Math.trunc(index), 0), pageCount - 1);
}

export function pageIndexById(pages: QamPage[], id: string): number {
  const index = pages.findIndex((page) => page.id === id);
  return index >= 0 ? index : 0;
}

export function getPreviousPageIndex(index: number, pageCount: number): number {
  return Math.max(0, clampPageIndex(index, pageCount) - 1);
}

export function getNextPageIndex(index: number, pageCount: number): number {
  if (pageCount <= 0) {
    return 0;
  }
  return Math.min(pageCount - 1, clampPageIndex(index, pageCount) + 1);
}

// -- QAM page session ---------------------------------------------------------
// Steam may transiently remount the plugin content (e.g. when a Dropdown
// context menu opens/closes). The active page must survive that transient
// remount but reset when the QAM genuinely closes. This mirrors the Region
// editor draft-session lifetime (see `regionEditor.ts`). In-memory only: no
// localStorage/sessionStorage/backend.

let sessionActivePageId: string | null = null;

export function getQamPageSession(): string | null {
  return sessionActivePageId;
}

export function rememberQamPageSession(id: string): void {
  sessionActivePageId = id;
}

export function resetQamPageSession(): void {
  sessionActivePageId = null;
}

// Resolve a stored page id against the current page list, falling back to the
// first page (OCR) for null/stale/unknown ids.
export function resolveSessionPageId(pages: QamPage[], stored: string | null): string {
  if (stored && pages.some((page) => page.id === stored)) {
    return stored;
  }
  return pages[0]?.id ?? "";
}

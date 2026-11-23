/** Accept only absolute https URLs for untrusted hrefs (e.g. lake source PDFs). */
export function safeHttpsUrl(url: string | undefined | null): string | null {
  if (url == null || typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!trimmed) return null;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "https:") return null;
    return parsed.href;
  } catch {
    return null;
  }
}
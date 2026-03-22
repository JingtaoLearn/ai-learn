const EMS_BASE_URL = process.env.EMS_BASE_URL || 'http://127.0.0.1:8100';

function emsHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = process.env.EMS_AUTH_TOKEN;
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return headers;
}

export type EmsVerdict = 'block' | 'warn' | 'pass';

export interface EmsCheckResult {
  verdict: EmsVerdict;
  reason?: string;
  available: boolean;
}

export interface EmsLearnResult {
  draftId: string;
  available: boolean;
}

export async function emsCheck(stepGoal: string, rules: string[]): Promise<EmsCheckResult> {
  try {
    const action = rules.length > 0
      ? `${stepGoal}. Rules to follow: ${rules.join(', ')}.`
      : stepGoal;

    const response = await fetch(`${EMS_BASE_URL}/api/check`, {
      method: 'POST',
      headers: emsHeaders(),
      body: JSON.stringify({ action }),
      signal: AbortSignal.timeout(5000),
    });

    if (!response.ok) {
      console.warn(`[ems] Check returned non-OK status: ${response.status}`);
      return { verdict: 'pass', available: true };
    }

    const data = await response.json() as { verdict: EmsVerdict; reason?: string };
    return { verdict: data.verdict, reason: data.reason, available: true };
  } catch (err) {
    console.warn(`[ems] EMS unavailable for check: ${err instanceof Error ? err.message : String(err)}`);
    return { verdict: 'pass', available: false };
  }
}

export async function emsLearn(content: string, context: string): Promise<EmsLearnResult> {
  try {
    const response = await fetch(`${EMS_BASE_URL}/api/learn`, {
      method: 'POST',
      headers: emsHeaders(),
      body: JSON.stringify({ content, context }),
      signal: AbortSignal.timeout(5000),
    });

    if (!response.ok) {
      throw new Error(`EMS learn failed: ${response.status}`);
    }

    const data = await response.json() as { id: string };
    return { draftId: data.id, available: true };
  } catch (err) {
    console.warn(`[ems] EMS unavailable for learn: ${err instanceof Error ? err.message : String(err)}`);
    return { draftId: '', available: false };
  }
}

export async function emsConfirm(draftId: string): Promise<boolean> {
  try {
    const response = await fetch(`${EMS_BASE_URL}/api/learn/confirm`, {
      method: 'POST',
      headers: emsHeaders(),
      body: JSON.stringify({ id: draftId }),
      signal: AbortSignal.timeout(5000),
    });
    return response.ok;
  } catch (err) {
    console.warn(`[ems] EMS confirm failed: ${err instanceof Error ? err.message : String(err)}`);
    return false;
  }
}

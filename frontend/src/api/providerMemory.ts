/**
 * The connected provider session for this page lifetime only.
 *
 * Only the server-side handle and its non-secret description are kept, in memory. The API key never
 * reaches this module. A reload or server restart forgets the handle, so the user is asked to reconnect
 * instead of inheriting a credential silently.
 */

import { client } from './client'
import type { ProviderResponse } from './types'

let current: ProviderResponse | null = null

export function rememberProvider(provider: ProviderResponse | null): void {
  current = provider
}

/** The remembered provider if the server still holds its session, otherwise null. */
export async function liveProvider(): Promise<ProviderResponse | null> {
  if (!current) return null
  try {
    const checked = await client.getProvider(current.provider_session_id)
    current = checked
    return checked
  } catch {
    current = null
    return null
  }
}

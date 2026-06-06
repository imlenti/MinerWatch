import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ShieldAlert, X } from 'lucide-react';

import { useAuthStatus } from '@/api/hooks';

// First-run security nudge.
//
// Renders only when the backend reports `needs_setup` — i.e. the dashboard
// is reachable from the network (non-loopback bind) but the control
// endpoints are NOT protected by a password. It is purely informational:
// it changes no behaviour and blocks nothing. The real hardening (mandatory
// password, write-endpoint gating) is a separate, staged step.
//
// If the field is missing (older backend) `needs_setup` is undefined and the
// banner stays hidden — safe by omission.
//
// Dismiss is session-only (in-memory): we deliberately avoid persisting it,
// so a still-unprotected install reminds the operator again on the next
// visit until they actually set a password.
export function SecurityBanner() {
  const { data: status } = useAuthStatus();
  const [dismissed, setDismissed] = useState(false);

  if (!status?.needs_setup || dismissed) return null;

  return (
    <div
      role="alert"
      className="mb-4 flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-300"
    >
      <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-medium">Your miners are unprotected</p>
        <p className="mt-0.5 text-amber-700/90 dark:text-amber-300/90">
          Anyone on your network can view <em>and control</em> your miners —
          including changing frequency/voltage and pool payout. Set a password
          to lock down the controls.{' '}
          <Link
            to="/settings"
            className="font-medium underline underline-offset-2 hover:text-amber-800 dark:hover:text-amber-200"
          >
            Set a password
          </Link>
        </p>
      </div>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        aria-label="Dismiss"
        className="shrink-0 rounded p-0.5 text-amber-700/70 hover:text-amber-800 dark:text-amber-300/70 dark:hover:text-amber-200"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ShieldAlert } from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Called when the operator explicitly chooses to scan while leaving the
  // miners unprotected (checkbox ticked + "Scan anyway"). The caller
  // persists the opt-out and runs the scan.
  onProceed: () => void;
}

// Just-in-time security gate, shown the first time the user triggers an
// auto-scan while the install is unprotected (needs_setup && !scan_ack).
// Unlike the ambient banner this blocks the action, so the warning is
// actually read. Two ways out:
//   - "Set a password" → Security settings (the safe path).
//   - tick "I don't want to protect my miners" → "Scan anyway" enables;
//     proceeding records the opt-out so we don't nag on every scan.
export function SecurityScanDialog({ open, onOpenChange, onProceed }: Props) {
  const [acknowledged, setAcknowledged] = useState(false);

  // Reset the tick whenever the dialog (re)opens, so a previous choice
  // doesn't carry into a fresh prompt.
  useEffect(() => {
    if (open) setAcknowledged(false);
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-amber-500" />
            Your miners are unprotected
          </DialogTitle>
          <DialogDescription>
            Auto-scan finds and exposes your fleet on the network. Right now
            anyone on your network can not only view your miners but also{' '}
            <span className="font-medium text-foreground">control</span> them —
            change frequency/voltage (which can damage hardware) or redirect
            your pool payout. Setting a password locks the controls down.
          </DialogDescription>
        </DialogHeader>

        <label className="flex items-start gap-2 rounded-md border border-border bg-muted/30 px-3 py-2 text-sm">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(e) => setAcknowledged(e.target.checked)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-amber-500"
          />
          <span>
            I don't want to protect my miners — scan anyway and don't ask again.
          </span>
        </label>

        <DialogFooter>
          <Button
            variant="ghost"
            disabled={!acknowledged}
            onClick={() => onProceed()}
          >
            Scan anyway
          </Button>
          <Button asChild>
            <Link to="/settings?tab=security" onClick={() => onOpenChange(false)}>
              Set a password
            </Link>
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

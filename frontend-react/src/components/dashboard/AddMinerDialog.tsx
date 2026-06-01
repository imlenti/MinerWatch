import { useState } from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAddMiner } from '@/api/hooks';
import { ApiError } from '@/lib/api';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Manual "Add miner" form. Submits to POST /api/miners.
 *
 * Address-only by design: the user types an IP or hostname and the
 * backend connects to the miner to auto-detect the family, port, MAC,
 * model and a friendly name -- the same fingerprint auto-discovery runs.
 * That removes the old family dropdown (whose `bitaxe` default mis-saved
 * NerdOctaxe / NerdQAxe boards) and the manual port field (ports 80 /
 * 4028 are probed automatically).
 *
 * The probe is best-effort with a hard stop: if the miner doesn't
 * answer, the backend returns 400 and we surface its message instead of
 * registering an unreachable device. Notes is the one optional field.
 */
export function AddMinerDialog({ open, onOpenChange }: Props) {
  const [host, setHost] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState<string | null>(null);

  const addMiner = useAddMiner();

  function reset() {
    setHost('');
    setNotes('');
    setError(null);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (!host.trim()) {
      setError('Host or IP is required.');
      return;
    }
    try {
      await addMiner.mutateAsync({
        host: host.trim(),
        notes: notes.trim() || null,
      });
      reset();
      onOpenChange(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) reset();
        onOpenChange(o);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add miner</DialogTitle>
          <DialogDescription>
            Enter the miner&apos;s IP or hostname. MinerWatch connects to it and detects the
            model, type and port automatically. The device must be powered on and reachable.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="host">Host or IP *</Label>
            <Input
              id="host"
              type="text"
              placeholder="192.168.1.42  or  bitaxe-supra.local"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              required
              autoFocus
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="notes">Notes (optional)</Label>
            <Input
              id="notes"
              type="text"
              placeholder="Free text -- visible on the miner detail page"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                reset();
                onOpenChange(false);
              }}
              disabled={addMiner.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={addMiner.isPending || !host.trim()}>
              {addMiner.isPending ? 'Connecting...' : 'Add miner'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

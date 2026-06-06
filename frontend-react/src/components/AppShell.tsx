import { useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  Activity,
  BarChart3,
  Cpu,
  Download,
  Heart,
  Menu,
  Moon,
  Network,
  Radio,
  Server,
  Settings as SettingsIcon,
  Sun,
  X,
} from 'lucide-react';

import { cn } from '@/lib/utils';
import { useTheme } from '@/lib/useTheme';
import { useUpdateCheck } from '@/api/hooks';
import { SecurityBanner } from '@/components/SecurityBanner';

// MinerWatch's persistent shell:
//   - on >= md (≥ 768 px) a 240 px sidebar on the left with the four
//     main nav entries, plus a slot for the page content on the right.
//   - on < md (phones) the sidebar collapses to a fixed top bar with a
//     hamburger that opens a slide-in drawer. The drawer carries the
//     same NAV_ITEMS so nothing is lost on mobile.
//
// Every route inside AppShell renders into <Outlet />, so individual
// pages don't have to redeclare the chrome.

// Two kinds of sidebar entries:
//   - LinkNavItem renders as a <NavLink>, taking the user to a route.
//   - ActionNavItem renders as a <button>, firing a callback (used for
//     the Donate entry, which opens a modal instead of navigating).
// The discriminator is the `kind` field.
interface BaseNavItem {
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  description: string;
  /** Tailwind class for the icon's *resting* colour, overriding the
   *  default muted-foreground (used to keep the Donate heart red). */
  iconClassName?: string;
  /** When true, render a small dot indicator next to the label (used
   *  for "update available" on the Update entry). */
  badge?: boolean;
}

interface LinkNavItem extends BaseNavItem {
  kind: 'link';
  to: string;
  end?: boolean;
}

interface ActionNavItem extends BaseNavItem {
  kind: 'action';
  onClick: () => void;
}

type NavItem = LinkNavItem | ActionNavItem;

interface NavListProps {
  onNavigate?: () => void;
  updateAvailable: boolean;
}

function NavList({ onNavigate, updateAvailable }: NavListProps) {
  const items: NavItem[] = [
    {
      kind: 'link',
      to: '/',
      label: 'Dashboard',
      icon: Activity,
      description: 'Fleet overview',
      end: true,
    },
    {
      kind: 'link',
      to: '/analytics',
      label: 'Analytics',
      icon: BarChart3,
      description: 'Predictions & records',
    },
    {
      kind: 'link',
      to: '/live',
      label: 'Live shares',
      icon: Radio,
      description: 'Real-time per-share stream',
    },
    {
      kind: 'link',
      to: '/pools',
      label: 'Pools',
      icon: Network,
      description: 'Stratum endpoints & shares',
    },
    {
      kind: 'link',
      to: '/settings',
      label: 'Settings',
      icon: SettingsIcon,
      description: 'Configuration',
    },
    {
      kind: 'link',
      to: '/system',
      label: 'System',
      icon: Server,
      description: 'Host metrics',
    },
    {
      kind: 'link',
      to: '/update',
      label: 'Update',
      icon: Download,
      description: 'Check for new versions',
      badge: updateAvailable,
    },
    {
      kind: 'link',
      to: '/donations',
      label: 'Donate',
      icon: Heart,
      description: 'Support MinerWatch',
      // Red outline only (lucide Heart is outline by default).
      iconClassName: 'text-red-500',
    },
  ];

  // Shared inner layout for both kinds, so the action button and the
  // NavLink stay visually identical (same paddings, gaps, typography).
  const renderInner = (
    item: NavItem,
    state: { isActive: boolean; isHover: boolean },
  ) => (
    <>
      <item.icon
        className={cn(
          'h-4 w-4 shrink-0',
          item.iconClassName ??
            (state.isActive
              ? 'text-primary'
              : 'text-muted-foreground group-hover:text-foreground'),
        )}
      />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="flex items-center gap-2 font-medium">
          {item.label}
          {item.badge && (
            <span
              aria-label="Update available"
              className="inline-block h-1.5 w-1.5 rounded-full bg-red-500"
            />
          )}
        </span>
        <span className="text-[11px] text-muted-foreground">{item.description}</span>
      </div>
    </>
  );

  return (
    <nav className="flex-1 space-y-1">
      {items.map((item) => {
        if (item.kind === 'link') {
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  'group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors',
                  isActive
                    ? 'bg-primary/15 text-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground',
                )
              }
            >
              {({ isActive }) =>
                renderInner(item, { isActive, isHover: false })
              }
            </NavLink>
          );
        }
        return (
          <button
            key={item.label}
            type="button"
            onClick={item.onClick}
            className={cn(
              'group flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors',
              'text-muted-foreground hover:bg-accent hover:text-foreground',
            )}
          >
            {renderInner(item, { isActive: false, isHover: false })}
          </button>
        );
      })}
    </nav>
  );
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={cn('flex items-center gap-3', compact ? '' : 'px-2')}>
      <div
        className={cn(
          'flex items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold',
          compact ? 'h-8 w-8' : 'h-9 w-9',
        )}
      >
        <Cpu className={cn(compact ? 'h-4 w-4' : 'h-5 w-5')} />
      </div>
      <div>
        <div className={cn('font-semibold leading-tight', compact && 'text-sm')}>MinerWatch</div>
        {!compact && (
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
            Mining monitor
          </div>
        )}
      </div>
    </div>
  );
}

function Footer({ version }: { version?: string }) {
  // Until the /api/version response lands we still want *something* in
  // the corner — fall back to "·" so the line keeps its height and the
  // layout doesn't pop when the value arrives.
  const display = version ? `v${version} · local` : '· local';
  return (
    <div className="border-t border-border pt-4 text-[11px] text-muted-foreground">
      <div className="font-mono">{display}</div>
      <div className="mt-1">No cloud · AGPL-3.0</div>
    </div>
  );
}

// Theme toggle button. Lives in the desktop sidebar header and in the
// mobile top bar so it's reachable on both layouts without taking up
// a nav slot. The icon mirrors the *current* theme (Sun on dark,
// Moon on light) — clicking it switches to the other one.
function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const isDark = theme === 'dark';
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      className={cn(
        'inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring',
        className,
      )}
    >
      {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  );
}

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  // Update check: drives the red dot on the Update entry. Cached to
  // 30min on the client; the backend itself caches the GitHub API
  // response for 6h so we don't get rate-limited (60 req/h/IP for
  // anonymous calls). We don't surface errors here — a failed check
  // just leaves the badge off.
  const { data: updateInfo } = useUpdateCheck();
  const updateAvailable = Boolean(updateInfo?.available);
  const version = updateInfo?.current;

  // Close the mobile drawer whenever the route changes — otherwise the
  // user taps a link and the drawer stays open over the new page.
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      {/* Desktop sidebar — hidden on phones */}
      <aside className="hidden md:flex w-60 flex-col border-r border-border bg-card/50 px-4 py-6">
        <div className="mb-8 flex items-center justify-between">
          <Brand />
          <ThemeToggle />
        </div>
        <NavList updateAvailable={updateAvailable} />
        <Footer version={version} />
      </aside>

      {/* Mobile top bar — only on phones. Fixed so it stays visible
          while the user scrolls long pages like Settings. */}
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center justify-between border-b border-border bg-card/95 px-4 backdrop-blur md:hidden">
        <Brand compact />
        <div className="flex items-center gap-1">
          <ThemeToggle className="h-10 w-10" />
          <button
            type="button"
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation menu"
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-foreground hover:bg-accent focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <Menu className="h-5 w-5" />
          </button>
        </div>
      </header>

      {/* Mobile drawer (Radix Dialog used as a side sheet) */}
      <DialogPrimitive.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay
            className={cn(
              'fixed inset-0 z-50 bg-black/70 backdrop-blur-sm md:hidden',
              'data-[state=open]:animate-in data-[state=closed]:animate-out',
              'data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
            )}
          />
          <DialogPrimitive.Content
            className={cn(
              'fixed inset-y-0 left-0 z-50 flex h-full w-72 max-w-[85vw] flex-col',
              'border-r border-border bg-card px-4 py-6 shadow-xl md:hidden',
              'data-[state=open]:animate-in data-[state=closed]:animate-out',
              'data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left',
              'duration-200',
            )}
          >
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">
              MinerWatch main navigation menu
            </DialogPrimitive.Description>

            <div className="mb-6 flex items-center justify-between">
              <Brand />
              <DialogPrimitive.Close
                aria-label="Close navigation menu"
                className="inline-flex h-9 w-9 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              >
                <X className="h-4 w-4" />
              </DialogPrimitive.Close>
            </div>

            <NavList
              onNavigate={() => setMobileOpen(false)}
              updateAvailable={updateAvailable}
            />
            <Footer version={version} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <main className="flex-1 min-w-0 px-4 pb-6 pt-20 md:px-8 md:py-8 md:pt-8">
        <SecurityBanner />
        <Outlet />
      </main>
    </div>
  );
}

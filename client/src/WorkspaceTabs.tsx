import type { ReactNode } from "react";

export type WorkspaceTabKey = "content" | "people" | "launch";

export type WorkspaceTabBadge =
  | { kind: "ready" }
  | { kind: "missing"; count: number };

export type WorkspaceTab = {
  badge: WorkspaceTabBadge;
  content: ReactNode;
  key: WorkspaceTabKey;
  label: string;
};

function badgeText(badge: WorkspaceTabBadge) {
  if (badge.kind === "ready" || badge.count === 0) {
    return "✓";
  }
  return `缺失 ${badge.count} 项`;
}

export function WorkspaceTabs({
  activeKey,
  busy,
  onTabChange,
  readOnly,
  tabs,
}: {
  activeKey: WorkspaceTabKey;
  busy: boolean;
  onTabChange: (key: WorkspaceTabKey) => void;
  readOnly: boolean;
  tabs: WorkspaceTab[];
}) {
  return (
    <div className="workspace-tabs">
      <div
        aria-label="工作区配置标签页"
        className="workspace-tab-list"
        role="tablist"
      >
        {tabs.map((tab) => {
          const isActive = tab.key === activeKey;
          const isReady = tab.badge.kind === "ready" || tab.badge.count === 0;
          return (
            <button
              aria-controls={`workspace-tabpanel-${tab.key}`}
              aria-selected={isActive}
              className={
                isActive
                  ? "workspace-tab workspace-tab--active"
                  : "workspace-tab"
              }
              disabled={readOnly || busy}
              id={`workspace-tab-${tab.key}`}
              key={tab.key}
              onClick={() => onTabChange(tab.key)}
              role="tab"
              type="button"
            >
              <span className="workspace-tab__label">{tab.label}</span>
              <span
                className={
                  isReady
                    ? "workspace-tab__badge workspace-tab__badge--ready"
                    : "workspace-tab__badge workspace-tab__badge--missing"
                }
              >
                {badgeText(tab.badge)}
              </span>
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => {
        const isActive = tab.key === activeKey;
        return (
          <div
            aria-labelledby={`workspace-tab-${tab.key}`}
            className={
              isActive
                ? "workspace-tab-panel"
                : "workspace-tab-panel workspace-tab-panel--inactive"
            }
            id={`workspace-tabpanel-${tab.key}`}
            key={tab.key}
            role="tabpanel"
          >
            {tab.content}
          </div>
        );
      })}
    </div>
  );
}

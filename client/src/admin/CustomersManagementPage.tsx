import { useState } from "react";

import type { AdminActorInfo } from "../api.admin";
import { AdminActivationSection } from "./AdminActivationSection";
import { CustomersPage } from "./CustomersPage";
import { DevicesPage } from "./DevicesPage";
import { SessionsPage } from "./SessionsPage";
import { TabBar } from "./ui/TabBar";

const tabs = [
  { id: "customers", label: "客户列表" },
  { id: "codes", label: "激活码" },
  { id: "devices", label: "设备与会话" },
];

/**
 * v4 导航合并 — 客户管理：客户列表、激活码、设备与会话合并为一个菜单项。
 * 「查看设备」「查看会话」从客户行进入时自动切到设备与会话页签并携带客户上下文；
 * 激活码页签保留现有激活码管理区（创建即激活，快速发码表单由后续任务接入）。
 */
export function CustomersManagementPage({
  actor,
  readOnly = false,
  onSessionExpired,
  initialTab = "customers",
  initiallyShowGenerator = false,
}: {
  actor: AdminActorInfo;
  readOnly?: boolean;
  onSessionExpired: () => void;
  initialTab?: "customers" | "codes" | "devices";
  initiallyShowGenerator?: boolean;
}) {
  const [tab, setTab] = useState<string>(initialTab);
  const [showGenerator, setShowGenerator] = useState(initiallyShowGenerator);
  const [selectedUserId, setSelectedUserId] = useState<string | undefined>();
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="客户管理页签"
        items={tabs}
        onChange={setTab}
        actions={
          tab === "codes" && !readOnly ? (
            <button
              type="button"
              onClick={() => setShowGenerator((open) => !open)}
            >
              {showGenerator ? "收起生成表单" : "生成激活码"}
            </button>
          ) : undefined
        }
      />
      {tab === "customers" ? (
        <CustomersPage
          embedded
          readOnly={readOnly}
          onOpenDevices={(userId) => {
            setSelectedUserId(userId);
            setTab("devices");
          }}
          onOpenSessions={(userId) => {
            setSelectedUserId(userId);
            setTab("devices");
          }}
        />
      ) : null}
      {tab === "codes" ? (
        <AdminActivationSection
          actor={actor}
          onSessionExpired={onSessionExpired}
          showGenerator={showGenerator}
        />
      ) : null}
      {tab === "devices" ? (
        <div className="admin-devices-sessions-layout">
          <DevicesPage readOnly={readOnly} userId={selectedUserId} />
          <SessionsPage
            readOnly={readOnly}
            userId={selectedUserId}
            onCustomerChange={setSelectedUserId}
          />
        </div>
      ) : null}
    </div>
  );
}

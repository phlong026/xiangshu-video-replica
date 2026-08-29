import { useState } from "react";

import type { AdminActorInfo } from "../api.admin";
import { ActivationCodeBatchesPage } from "./ActivationCodeBatchesPage";
import { ActivationCodesPage } from "./ActivationCodesPage";
import { DeliveriesPage } from "./DeliveriesPage";

type SubPage = "batches" | "codes" | "deliveries";

type AdminActivationSectionProps = {
  actor: AdminActorInfo;
  unitPriceFen: number | null;
  onSessionExpired: () => void;
};

const subPages: Array<{ id: SubPage; label: string }> = [
  { id: "batches", label: "生成激活码" },
  { id: "codes", label: "激活码列表" },
  { id: "deliveries", label: "激活码发放" },
];

/** Activation-code pages inside the application-wide administrator session. */
export function AdminActivationSection({
  actor,
  unitPriceFen,
  onSessionExpired,
}: AdminActivationSectionProps) {
  const [activePage, setActivePage] = useState<SubPage>("batches");
  const readOnly = actor.role === "auditor";

  return (
    <section className="admin-panel" aria-label="激活码管理">
      {readOnly ? (
        <p className="wallet-notice" role="status">
          审计员只读：仅可查看，不能执行写操作。
        </p>
      ) : null}

      <nav className="admin-tabs" aria-label="激活码管理导航">
        {subPages.map((page) => (
          <button
            aria-current={activePage === page.id ? "page" : undefined}
            className={
              activePage === page.id ? "admin-tab is-active" : "admin-tab"
            }
            key={page.id}
            type="button"
            onClick={() => setActivePage(page.id)}
          >
            {page.label}
          </button>
        ))}
      </nav>

      {activePage === "batches" ? (
        <ActivationCodeBatchesPage
          readOnly={readOnly}
          unitPriceFen={unitPriceFen}
          onSessionExpired={onSessionExpired}
        />
      ) : null}
      {activePage === "codes" ? (
        <ActivationCodesPage
          readOnly={readOnly}
          onSessionExpired={onSessionExpired}
        />
      ) : null}
      {activePage === "deliveries" ? (
        <DeliveriesPage
          readOnly={readOnly}
          onSessionExpired={onSessionExpired}
        />
      ) : null}
    </section>
  );
}

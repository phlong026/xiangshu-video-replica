import { useState } from "react";

import type { AdminActorInfo } from "../api.admin";
import { ActivationCodeBatchesPage } from "./ActivationCodeBatchesPage";
import { ActivationCodesPage } from "./ActivationCodesPage";
import { DeliveriesPage } from "./DeliveriesPage";
import { PageBanner } from "./ui/PageBanner";

type AdminActivationSectionProps = {
  actor: AdminActorInfo;
  onSessionExpired: () => void;
};

/** Activation-code pages inside the application-wide administrator session. */
export function AdminActivationSection({
  actor,
  onSessionExpired,
}: AdminActivationSectionProps) {
  const [refreshToken, setRefreshToken] = useState(0);
  const readOnly = actor.role === "auditor";

  return (
    <section className="admin-panel" aria-label="激活码管理">
      {readOnly ? (
        <PageBanner tone="notice">
          审计员只读：仅可查看，不能执行写操作。
        </PageBanner>
      ) : null}

      <ActivationCodeBatchesPage
        readOnly={readOnly}
        onGenerated={() => setRefreshToken((current) => current + 1)}
        onSessionExpired={onSessionExpired}
      />
      <ActivationCodesPage
        readOnly={readOnly}
        refreshToken={refreshToken}
        onSessionExpired={onSessionExpired}
      />
      <DeliveriesPage readOnly={readOnly} onSessionExpired={onSessionExpired} />
    </section>
  );
}

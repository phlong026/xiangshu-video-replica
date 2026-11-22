import { describe, expect, it } from "vitest";

import type { CustomerSubAccountPermissions } from "../api";
import { businessLabel } from "./businessLabels";
import {
  BUSINESS_FEATURES,
  isAllGranted,
  isPermissionRestricted,
  type PermissionDraft,
  permissionDraft,
  permissionSummary,
  permissionsPayload,
  toggleBusiness,
} from "./permissionViz";

/** 已有受限配置不应因新增业务而自动扩权。 */
const LEGACY_KEYS = [
  "video",
  "oral",
  "character",
  "first_frame",
  "analysis",
  "rewrite",
  "asr",
  "link_resolution",
  "prompt_optimize",
  "avatar_clone",
  "voice_clone",
  "viral_data",
] as const;
const ALL_KEYS = [...LEGACY_KEYS, "account_diagnosis"] as const;

describe("permissionViz", () => {
  it("keeps the business features in the backend declaration order", () => {
    expect(BUSINESS_FEATURES.map((feature) => feature.key)).toEqual(ALL_KEYS);
    expect(BUSINESS_FEATURES).toHaveLength(13);
    expect(BUSINESS_FEATURES[0]).toEqual({ key: "video", label: "视频生成" });
    expect(BUSINESS_FEATURES[11]).toEqual({
      key: "viral_data",
      label: "爆款数据",
    });
    expect(businessLabel("account_diagnosis")).toBe("账号诊断");
  });

  it("expands an unrestricted account (null) into a fully granted draft", () => {
    const draft = permissionDraft(null);
    expect(draft.businesses).toEqual(ALL_KEYS);
    expect(draft.allowApiKeys).toBe(true);
    expect(draft.allowPublishAccounts).toBe(true);
    expect(isAllGranted(draft)).toBe(true);
  });

  it("maps a stored restriction onto the draft in declaration order", () => {
    const draft = permissionDraft({
      // 乱序 + 只留两项，验证按声明顺序折叠而不是按数组顺序透传。
      businesses: ["viral_data", "video"],
      allow_api_keys: false,
      allow_publish_accounts: true,
    });
    expect(draft.businesses).toEqual(["video", "viral_data"]);
    expect(draft.allowApiKeys).toBe(false);
    expect(draft.allowPublishAccounts).toBe(true);
  });

  it("serialises the draft into the backend's three-field payload", () => {
    const draft: PermissionDraft = {
      businesses: ["rewrite", "video"],
      allowApiKeys: false,
      allowPublishAccounts: false,
    };
    expect(permissionsPayload(draft)).toEqual({
      businesses: ["video", "rewrite"],
      allow_api_keys: false,
      allow_publish_accounts: false,
    });
  });

  it("toggles a single business key without disturbing the order", () => {
    const draft = permissionDraft(null);
    const withoutOral = toggleBusiness(draft, "oral");
    expect(withoutOral.businesses).toEqual(
      ALL_KEYS.filter((key) => key !== "oral"),
    );
    const restored = toggleBusiness(withoutOral, "oral");
    expect(restored.businesses).toEqual(ALL_KEYS);
    // 原草稿不被就地修改（不可变更新）。
    expect(draft.businesses).toEqual(ALL_KEYS);
  });

  it("only reports full grants when all businesses and both switches are on", () => {
    const full = permissionDraft(null);
    expect(isAllGranted(full)).toBe(true);
    expect(isAllGranted({ ...full, businesses: [...LEGACY_KEYS] })).toBe(false);
    expect(isAllGranted({ ...full, allowApiKeys: false })).toBe(false);
    expect(isAllGranted({ ...full, allowPublishAccounts: false })).toBe(false);
  });

  it.each([
    [true, true],
    [false, true],
    [true, false],
  ])(
    "keeps legacy grants restricted without granting diagnosis (%s, %s)",
    (allowApiKeys, allowPublishAccounts) => {
      const permissions = {
        businesses: [...LEGACY_KEYS],
        allow_api_keys: allowApiKeys,
        allow_publish_accounts: allowPublishAccounts,
      };
      const draft = permissionDraft(permissions);
      expect(draft.businesses).toEqual(LEGACY_KEYS);
      expect(isAllGranted(draft)).toBe(false);
      expect(isPermissionRestricted(permissions)).toBe(true);
      expect(permissionsPayload(draft)).toEqual(permissions);
    },
  );

  it("summarises restrictions with counts and disabled switches", () => {
    expect(permissionSummary(null)).toBe("");
    const restricted: CustomerSubAccountPermissions = {
      businesses: [
        "video",
        "oral",
        "character",
        "first_frame",
        "analysis",
        "rewrite",
        "asr",
        "link_resolution",
        "prompt_optimize",
      ],
      allow_api_keys: false,
      allow_publish_accounts: false,
    };
    expect(permissionSummary(restricted)).toBe(
      "9/13 类业务 · Token 禁用 · 发布账号禁用",
    );
    // 仅关一个开关（业务全留）也要如实列出。
    expect(
      permissionSummary({
        businesses: [...ALL_KEYS],
        allow_api_keys: false,
        allow_publish_accounts: true,
      }),
    ).toBe("13/13 类业务 · Token 禁用");
  });

  it("distinguishes unrestricted (null) from a stored full-grant row", () => {
    expect(isPermissionRestricted(null)).toBe(false);
    expect(
      isPermissionRestricted({
        businesses: ["video"],
        allow_api_keys: true,
        allow_publish_accounts: true,
      }),
    ).toBe(true);
    // 防御：全开 row（正常不会被存储）按「不受限」展示，不误报徽章。
    expect(
      isPermissionRestricted({
        businesses: [...ALL_KEYS],
        allow_api_keys: true,
        allow_publish_accounts: true,
      }),
    ).toBe(false);
  });
});

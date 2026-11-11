import { describe, expect, it } from "vitest";
import type { ShotCard } from "./api";
import {
  formatShotCardsForClipboard,
  missingMotionShotIds,
  promptTextFromShotTableClipboard,
} from "./ShotCardEditor";

describe("missingMotionShotIds", () => {
  const withMotion: ShotCard = {
    shot_id: "s1",
    start_time: 0,
    end_time: 4,
    shot_type: "中景",
    composition: "人物居中",
    camera_motion: "固定",
    subject: "主讲人",
    action: "面向镜头讲解",
    scene: "室内",
    spoken_text: "",
    transition: "硬切",
    motion: {
      subject_motion_state: "STATIC",
      subject_direction: "in_place",
      subject_displacement: "无位移",
      hand_action: "双手自然下垂",
      camera_motion: "STATIC",
      relative_motion: "人物与摄影机相对静止",
    },
  };

  it("列出缺运动信息的镜头编号（旧版拆解）", () => {
    const legacy: ShotCard = {
      ...withMotion,
      shot_id: "s2",
      motion: undefined,
    };

    expect(missingMotionShotIds([withMotion, legacy])).toEqual(["s2"]);
  });

  it("全部镜头都有运动时返回空数组", () => {
    expect(missingMotionShotIds([withMotion])).toEqual([]);
  });

  it("镜头编号为空时按序号兜底命名", () => {
    const unnamed: ShotCard = {
      ...withMotion,
      shot_id: "  ",
      motion: undefined,
    };

    expect(missingMotionShotIds([unnamed])).toEqual(["第 1 镜"]);
  });
});

describe("formatShotCardsForClipboard", () => {
  it("输出可直接粘贴到表格的完整制表符分镜", () => {
    const shot: ShotCard = {
      shot_id: "镜头 1",
      start_time: 0,
      end_time: 3.5,
      shot_type: "中景",
      composition: "人物居中",
      camera_motion: "推进",
      subject: "设计师",
      action: "走向镜头\n并抬手",
      scene: "乡村院落",
      spoken_text: "这栋房子的采光很好",
      transition: "硬切",
      motion: {
        subject_motion_state: "WALKING",
        subject_direction: "toward_camera",
        subject_displacement: "约 2 米",
        hand_action: "抬右手",
        camera_motion: "FOLLOW",
        relative_motion: "镜头跟随人物",
      },
    };

    const output = formatShotCardsForClipboard([shot]);

    expect(output.split("\n")).toHaveLength(2);
    expect(output).toContain("镜头编号\t开始(秒)\t结束(秒)");
    expect(output).toContain("镜头 1\t0\t3.5\t中景");
    expect(output).toContain("人物：行走；方向：向镜头");
    expect(output).toContain("走向镜头 并抬手");
  });

  it("只接受复制按钮产生的分镜表，并包装为待转 H3 的提示词", () => {
    const imported = promptTextFromShotTableClipboard(
      "镜头编号\t开始(秒)\t结束(秒)\t动作\ns1\t0\t4\t面向镜头",
    );
    expect(imported).toEqual(
      expect.objectContaining({
        ok: true,
        promptText: expect.stringContaining("H3"),
      }),
    );

    expect(promptTextFromShotTableClipboard("普通文案")).toEqual({
      ok: false,
      error: "未识别到分镜表表头，请先使用拆解页面的“复制分镜表”按钮。",
    });
    expect(
      promptTextFromShotTableClipboard("镜头编号\t开始(秒)\t结束(秒)"),
    ).toEqual({
      ok: false,
      error: "分镜表中没有可导入的镜头数据。",
    });
  });
});

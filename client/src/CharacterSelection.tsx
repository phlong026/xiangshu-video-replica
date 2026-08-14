import { useState } from "react";

import {
  type Character,
  chooseProjectMainCharacter,
  getProjectMainCharacter,
  listProjectCharacters,
} from "./api";

export function CharacterSelection({ projectId }: { projectId: string }) {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [selectedCharacterId, setSelectedCharacterId] = useState("");
  const [selectedCharacterName, setSelectedCharacterName] = useState("");
  const [isOpen, setIsOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  async function openSelection() {
    setIsOpen(true);
    setIsLoading(true);
    setError("");
    try {
      const [availableCharacters, currentCharacter] = await Promise.all([
        listProjectCharacters(projectId),
        getProjectMainCharacter(projectId),
      ]);
      setCharacters(availableCharacters);
      if (currentCharacter) {
        setSelectedCharacterId(currentCharacter.character_id);
        setSelectedCharacterName(
          currentCharacter.character_snapshot.name ?? "",
        );
      }
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "读取可用人物失败。",
      );
    } finally {
      setIsLoading(false);
    }
  }

  async function saveSelection() {
    if (!selectedCharacterId) {
      return;
    }
    setIsSaving(true);
    setError("");
    setMessage("");
    try {
      const selection = await chooseProjectMainCharacter(
        projectId,
        selectedCharacterId,
      );
      const name =
        characters.find((character) => character.id === selection.character_id)
          ?.name ??
        selection.character_snapshot.name ??
        "所选人物";
      setSelectedCharacterName(name);
      setMessage(`已选择人物“${name}”。`);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "选择人物失败。",
      );
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <section className="character-selection" aria-labelledby="character-title">
      <div className="section-heading">
        <div>
          <span className="eyebrow">CHARACTER LIBRARY</span>
          <h3 id="character-title">人物设定</h3>
          <p>
            {selectedCharacterName
              ? `当前人物：${selectedCharacterName}`
              : "选择已获当前项目授权的人物，用于下一步人物置换首帧。"}
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={openSelection}
          type="button"
        >
          选择人物
        </button>
      </div>
      {isOpen ? (
        <div className="character-selection-panel">
          {isLoading ? <p className="status-note">正在读取可用人物</p> : null}
          {error ? <p className="settings-error">{error}</p> : null}
          {!isLoading && !error && !characters.length ? (
            <p className="status-note">
              当前项目没有可用人物，请联系管理员维护人物库授权。
            </p>
          ) : null}
          {!isLoading && !error && characters.length ? (
            <fieldset className="character-options">
              <legend>选择一个主角</legend>
              {characters.map((character) => (
                <label key={character.id}>
                  <input
                    checked={selectedCharacterId === character.id}
                    name="main-character"
                    onChange={() => {
                      setSelectedCharacterId(character.id);
                      setMessage("");
                    }}
                    type="radio"
                    value={character.id}
                  />
                  <span>{character.name}</span>
                  <small>{character.reference_asset_ids.length} 张参考图</small>
                </label>
              ))}
            </fieldset>
          ) : null}
          {message ? <p className="setup-success">{message}</p> : null}
          <button
            disabled={isLoading || isSaving || !selectedCharacterId}
            onClick={saveSelection}
            type="button"
          >
            {isSaving ? "正在保存" : "确认使用人物"}
          </button>
        </div>
      ) : null}
    </section>
  );
}

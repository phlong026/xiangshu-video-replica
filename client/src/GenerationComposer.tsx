import { useEffect } from "react";

import type { GenerationBatch } from "./api";
import { GenerationLauncher } from "./GenerationLauncher";
import { ScriptEditor } from "./ScriptEditor";
import { useGenerationDrafts } from "./useGenerationDrafts";

type GenerationComposerProps = {
  analysisVersionId: string;
  characterVersionId: string | null;
  currentUserId: string;
  durationSeconds: number;
  firstFrameAssetId: string;
  firstFrameSelectionVersionId: string;
  onBatchCreated: (batch: GenerationBatch) => void;
  onBusyChange: (isBusy: boolean) => void;
  onWorkflowStepChange: (step: number) => void;
  originalScript: string;
  projectId: string;
  readOnly?: boolean;
  referenceSelectionId: string | null;
  shotCardVersionId: string;
};

export function GenerationComposer({
  analysisVersionId,
  characterVersionId,
  currentUserId,
  durationSeconds,
  firstFrameAssetId,
  firstFrameSelectionVersionId,
  onBatchCreated,
  onBusyChange,
  onWorkflowStepChange,
  originalScript,
  projectId,
  readOnly = false,
  referenceSelectionId,
  shotCardVersionId,
}: GenerationComposerProps) {
  const drafts = useGenerationDrafts({
    characterVersionId,
    currentUserId,
    durationSeconds,
    firstFrameAssetId,
    firstFrameSelectionVersionId,
    originalScript,
    projectId,
    readOnly,
    referenceSelectionId,
    shotCardVersionId,
  });

  const { busyAction, workflowStep } = drafts;

  useEffect(() => {
    onBusyChange(Boolean(busyAction));
  }, [busyAction, onBusyChange]);

  useEffect(
    () => () => {
      onBusyChange(false);
    },
    [onBusyChange],
  );

  useEffect(() => {
    onWorkflowStepChange(workflowStep);
  }, [onWorkflowStepChange, workflowStep]);

  if (drafts.isLoading) {
    return <p className="status-note">正在读取口播稿与 Prompt</p>;
  }

  return (
    <section
      className="generation-composer"
      aria-labelledby="generation-composer-title"
    >
      <div className="section-heading">
        <div>
          <h3 id="generation-composer-title">口播稿与 Prompt</h3>
        </div>
      </div>

      {drafts.error ? <p className="settings-error">{drafts.error}</p> : null}
      {drafts.message ? (
        <p className="setup-success">{drafts.message}</p>
      ) : null}

      <ScriptEditor
        busyAction={busyAction}
        onChooseSource={drafts.chooseScriptSource}
        onSaveScript={drafts.saveScript}
        onScriptTextChange={drafts.setScriptText}
        readOnly={readOnly}
        scriptDirty={drafts.scriptDirty}
        scriptSource={drafts.scriptSource}
        scriptStale={drafts.scriptStale}
        scriptText={drafts.scriptText}
        shotMappings={drafts.shotMappings}
      />

      <GenerationLauncher
        analysisVersionId={analysisVersionId}
        busyAction={busyAction}
        canCompile={drafts.canCompile}
        canCreateBatch={drafts.canCreateBatch}
        characterVersionId={characterVersionId}
        durationValid={drafts.durationValid}
        firstFrameAssetId={firstFrameAssetId}
        firstFrameSelectionVersionId={firstFrameSelectionVersionId}
        limits={drafts.limits}
        onCompilePrompt={drafts.compilePrompt}
        onCreateBatch={() =>
          drafts.createBatch(onBatchCreated, onWorkflowStepChange)
        }
        onDurationChange={drafts.setOutputDuration}
        onLockPrompt={drafts.lockPrompt}
        onPromptTextChange={drafts.setPromptText}
        onQuantityChange={drafts.setQuantityInput}
        onRecoverBatch={() =>
          drafts.recoverBatch(onBatchCreated, onWorkflowStepChange)
        }
        onResolutionChange={drafts.setResolution}
        onSavePromptRevision={drafts.savePromptRevision}
        outputDuration={drafts.outputDuration}
        promptDirty={drafts.promptDirty}
        promptParametersMatch={drafts.promptParametersMatch}
        promptStale={drafts.promptStale}
        promptText={drafts.promptText}
        promptVersion={drafts.promptVersion}
        quantity={drafts.quantity}
        quantityError={drafts.quantityError}
        quantityInput={drafts.quantityInput}
        readOnly={readOnly}
        recoveryRecord={drafts.recoveryRecord}
        recoveryRecordConflicts={drafts.recoveryRecordConflicts}
        referenceSelectionId={referenceSelectionId}
        resolution={drafts.resolution}
        savedPromptText={drafts.savedPromptText}
        scriptStale={drafts.scriptStale}
        shotCardVersionId={shotCardVersionId}
      />
    </section>
  );
}

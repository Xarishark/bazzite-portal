import { definePlugin, routerHook } from "@decky/api";
import { Navigation } from "@decky/ui";
import { BazziteIcon } from "./BazziteIcon";
import { Title } from "./Title";
import { Panel } from "./Panel";
import { Logs, LOGS_ROUTE } from "./Logs";
import { bootedIdentity } from "./actions";
import { startState, usePortalState } from "./state";
import { messages as m } from "./messages";
import { closeOperationModals } from "./OperationModal";

export default definePlugin(() => {
  const stopState = startState();
  routerHook.addRoute(LOGS_ROUTE, Logs);
  return {
    name: m.app, titleView: <Title />, icon: <BazziteIcon />, content: <Panel />,
    onDismount() {
      closeOperationModals();
      stopState();
      routerHook.removeRoute(LOGS_ROUTE);
    },
  };
});

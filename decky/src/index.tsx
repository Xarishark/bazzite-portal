import { definePlugin, routerHook } from "@decky/api";
import { Navigation } from "@decky/ui";
import { BazziteIcon } from "./BazziteIcon";
import { Title } from "./Title";
import { Panel } from "./Panel";
import { bootedIdentity } from "./actions";
import { startState, usePortalState } from "./state";
import { messages as m } from "./messages";

export default definePlugin(() => {
  const stopState = startState();
  return {
    name: m.app, titleView: <Title />, icon: <BazziteIcon />, content: <Panel />,
    onDismount() {
      stopState();
    },
  };
});

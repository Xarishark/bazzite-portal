import { definePlugin } from "@decky/api";
import { BazziteIcon } from "./BazziteIcon";
import { messages as m } from "./messages";

export default definePlugin(() => ({name: m.app, title: m.app, icon: <BazziteIcon />, content: <div /> }));

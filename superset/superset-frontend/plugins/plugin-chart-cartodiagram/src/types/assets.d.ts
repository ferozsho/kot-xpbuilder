/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

/**
 * The plugin imports `src/images/loading.svg` from ChartLayer.tsx.
 *
 * The frontend app declares its asset modules in `src/types/files.d.ts`, but a
 * project reference (`tsc --build`) only sees the declarations inside its own
 * `include`, so the plugin needs its own copy or the build fails with
 * "TS2307: Cannot find module '../images/loading.svg'".
 */
declare module '*.svg' {
  const content: string;
  export default content;
}

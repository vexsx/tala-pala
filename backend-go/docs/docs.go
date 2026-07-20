// Package docs embeds the OpenAPI specification so the binary is fully
// self-contained.
package docs

import _ "embed"

//go:embed openapi.yaml
var OpenAPISpec []byte

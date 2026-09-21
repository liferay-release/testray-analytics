#!/bin/bash

function main {
	local credential_name="${1}"
	local field="${2}"

	local op_api_token=$(gcloud secrets versions access latest --secret="onepassword-api-token")
	local op_connect_url="http://release-master.us-west1-a.c.release-infra-20241029-1.internal:8080"

	local vault_id=$( \
		curl \
			--header "Content-Type: application/json" \
			--header "Authorization: Bearer ${op_api_token}" \
			--silent \
			"${op_connect_url}/v1/vaults" | \
		jq --raw-output ".[] | select(.name == \"Liferay Release-Secrets\") | .id")

	local credential_item_id=$( \
		curl \
			--header "Content-Type: application/json" \
			--header "Authorization: Bearer ${op_api_token}" \
			--silent \
			"${op_connect_url}/v1/vaults/${vault_id}/items" | \
		jq --raw-output ".[] | select(.title == \"${credential_name}\") | .id")

	local credential_field_value=$( \
		curl \
			--header "Content-Type: application/json" \
			--header "Authorization: Bearer ${op_api_token}" \
			--silent \
			"${op_connect_url}/v1/vaults/${vault_id}/items/${credential_item_id}" | \
		jq --raw-output ".fields[] | select(.label == \"${field}\") | .value")

	echo "${credential_field_value}"
}

main "$@"